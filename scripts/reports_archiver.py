#!/usr/bin/env python3
"""Fail-closed archival and retention for reports and generated local artifacts.

F12/F13 only remove an unchanged source after an exact local archive has been
verified off-host by checksum and size.  Site-tree planning additionally
requires the canonical current-content authority to validate.  Inspection is
strictly read-only; mutating paths are exercised only by explicit non-dry runs.
"""
from __future__ import annotations

import argparse
import base64
from bisect import bisect_left
import ctypes
import errno
import fcntl
import hashlib
import inspect
import io
import json
import os
import re
import select
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r2_offload  # noqa: E402
from glp_current_content_base_schema import is_prepared_pointer, validate_pointer_shape  # noqa: E402

BUSINESS = Path("/home/ubuntu/business")
HOME = Path("/home/ubuntu")
REPORTS = BUSINESS / "reports"
FLAGSHIP = BUSINESS / "site-system" / "flagship-v2"
ARCHIVE_ROOT = Path(
    os.environ.get("F12_ARCHIVE_ROOT", str(BUSINESS / "archive" / "f12-disk-archival"))
).expanduser()
HERMES_HOME = HOME / ".hermes"
STATE_SNAPSHOTS = HERMES_HOME / "state-snapshots"
SESSION_SPILLS = HERMES_HOME / "session-spills"
CRON_OUTPUT = HERMES_HOME / "cron" / "output"
CHECKPOINTS = HERMES_HOME / "checkpoints" / "store"
EXCLUDED_SCAN_ROOTS = (
    BUSINESS / "backups",
    HERMES_HOME / "browser-profiles",
    HERMES_HOME / "secrets",
)
RUNSTATE = HERMES_HOME / "runstate"
LOCK_PATH = RUNSTATE / "reports-archiver.lock"
ROLLBACK_REGISTRY_PATH = BUSINESS / "ops" / "f12-active-rollback-authority.json"
COMMON_LOCK_ABSTRACT_SOCKET_NAME = b"\0hermes-f12-f13-retention-archiver-v1"
OPS_ALERTS_CHANNEL_ID = "1487300132054765629"
OPS_ALERT = HERMES_HOME / "scripts" / "ops_alert.py"
EXPECTED_OPS_ALERT_SHA256 = "a9e0707b0c7e78c30414f70dd2895b8bed40c43c46528487933f04966b39599b"
OPS_ALERT_MAX_SOURCE_BYTES = 1024 * 1024
OPS_ALERT_BOOTSTRAP = (
    "import base64,sys;"
    "source=base64.b64decode(sys.argv[1],validate=True);"
    "source_path=sys.argv[2];message=sys.argv[3];"
    "sys.argv=[source_path,message];"
    "namespace={'__name__':'__main__','__file__':source_path,"
    "'__package__':None,'__cached__':None};"
    "exec(compile(source,source_path,'exec'),namespace,namespace)"
)

PROTECTED = (
    REPORTS / "loopos-runs",
    REPORTS / "glp-clinic-scout" / "affiliate-machine" / "g6-r1a-wirein-20260806",
    REPORTS / "glp-clinic-scout" / "seo-machine",
    REPORTS / "boring-ai" / "outreach-ops",
    FLAGSHIP / "releases" / "glp-g6r1a-wirein-20260806-r2",
    FLAGSHIP / "releases" / "glp-g6r1a-wirein-20260806-r1",
    FLAGSHIP / "candidates" / "glp-g6r1a-wirein-20260806-r2",
    FLAGSHIP / "candidates" / "glp-g6r1a-wirein-20260806-r1",
)

# Central candidate keepers.  F13 imports this exact tuple rather than keeping a
# second, drifting policy copy.
PROTECTED_CANDIDATE_PREFIXES = (
    "glp-g6r1a-wirein-",
    "glp-mounjaro-coupon-20260806-r10",
    "glp-mounjaro-coupon-20260806-r11",
)

GIB = 1024**3
MIN_REPORT_ALLOCATED = 100 * 1024**2
# Kept as a compatibility symbol for callers/tests.  It is deliberately zero
# and is never applied to site, state, cron, spill, or checkpoint lanes.
MIN_GENERATOR_ALLOCATED = 0
REPORT_RETENTION_DAYS = 30
SITE_LANE_RETENTION_SECONDS = {
    "releases": 14 * 86400,
    "candidates": 48 * 3600,
    "staging": 14 * 86400,
    "review-cuts": 24 * 3600,
}
# Release trees are canonical production/rollback evidence, never F12 removal
# targets.  Keep the lane in authority/reference parsing while excluding it
# from destructive discovery.
SITE_REMOVAL_LANES = frozenset({"candidates", "staging", "review-cuts"})
SITE_LANE_RETENTION_DAYS = {
    lane: seconds / 86400 for lane, seconds in SITE_LANE_RETENTION_SECONDS.items()
}
STATE_SNAPSHOT_RETENTION_DAYS = 30
SPILL_RETENTION_DAYS = 14
CRON_OUTPUT_RETENTION_DAYS = 30
CHECKPOINT_RETENTION_DAYS = 7
MIN_FREE_BYTES = 6 * GIB
SITE_REF_RE = re.compile(
    r"/home/ubuntu/business/site-system/flagship-v2/"
    r"(?:releases|candidates|staging|review-cuts)/[A-Za-z0-9_.-]+"
)
ABSOLUTE_REF_RE = re.compile(r"/[^\s\x00\"']+")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IGNORED_ACTIVITY_NAMES = {".pytest_cache", "__pycache__", "monitor.lock"}
IGNORED_ACTIVITY_SUFFIXES = (".pyc", ".pyo", ".shm", ".wal", ".lock")
AUTHORITY_MAX_BYTES = 5_000_000
ROLLBACK_SCAN_MAX_BYTES = 2_000_000
ROLLBACK_REGISTRY_SCHEMA = "f12-f13-retention-authority-registry-v2"
ROLLBACK_REGISTRY_IMMUTABLE_VERSION = 2
ROLLBACK_GENERATION_EVIDENCE_SCHEMA = (
    "f12-f13-retention-authority-generation-evidence-v1"
)
ROLLBACK_GENERATION_EVIDENCE_IMMUTABLE_VERSION = 1
ROLLBACK_UPDATE_PROVENANCE_SCHEMA = (
    "f12-f13-authority-explicit-update-provenance-v1"
)
ROLLBACK_REGISTRY_OWNER = "business-ops-f12-f13"
ROLLBACK_REGISTRY_AUTHORITY = "canonical_current_and_future_retention_pins"
ROLLBACK_REGISTRY_UPDATE_POLICY = "explicit_registry_update_only"
ROLLBACK_REGISTRY_UPDATE_MECHANISM = (
    "operator-authored-immutable-generation-evidence"
)
ROLLBACK_AUTOMATIC_RUNTIME_DISCOVERY_ALLOWED = False
ROLLBACK_REGISTRY_MAX_BYTES = 1_000_000
ROLLBACK_REGISTRY_MAX_ENTRIES = 256
ROLLBACK_REGISTRY_MAX_ROOTS_PER_ENTRY = 16
ROLLBACK_AUTHORITY_ENTRY_MAX_BYTES = 2_000_000
ROLLBACK_GENERATION_EVIDENCE_MAX_BYTES = 5_000_000
RECENT_REFERENCE_STATUS = "RECENT_REFERENCE_ACTIVE_UNTIL_EXPIRY"
RECENT_REFERENCE_EXPIRY_POLICY = "source_mtime_plus_86400_seconds"
RECENT_REFERENCE_WINDOW_SECONDS = 86400
ACTIVE_ROLLBACK_EXPIRY_POLICY = "NEVER"
HISTORICAL_BOOTSTRAP_PROVENANCE = {
    "active_entry_count": 0,
    "content_digest_rollup_sha256": (
        "a1c9b5cf7960eda05613a90d69456af042c8ec996a776cd95108825b7731a1ec"
    ),
    "evidence_path": (
        "/home/ubuntu/business/reports/f12-f13-batch-a-repair-20260808T171215Z/"
        "bootstrap-inventory-final.json"
    ),
    "inventory_ledger_sha256": (
        "8e000bd3ff13d14b13e52b1792668d3ab40811cf9e9514fed5b6957a9739bb02"
    ),
    "inventory_sha256": (
        "bb2837200e4e3e756571f9695db87a529f1d5b3d06ea2b0744d0cb00ad379a82"
    ),
    "json_file_count": 106335,
    "json_total_bytes": 7385204351,
    "malformed_json_count": 33,
    "method": "two-pass-complete-descriptor-bound-content-hash-and-semantic-json-inventory",
    "scanner_path": (
        "/home/ubuntu/business/reports/f12-f13-batch-a-repair-20260808T171215Z/"
        "bootstrap_inventory_scanner.py"
    ),
    "scanner_sha256": (
        "372a8b6fd2158815fbb3753e0206eb6efa58c9d1a87be450958e27dcac2f8a22"
    ),
    "symlinks_excluded_as_noncanonical": 23,
}
TERMINAL_RECEIPT_MAX_BYTES = 100_000_000
RECENT_REFERENCE_MAX_FILES = 25_000
RECENT_REFERENCE_MAX_TOTAL_BYTES = 1_000_000_000
INTERNAL_DEADLINE_MAX_SECONDS = 6600.0
OUTER_TIMEOUT_SECONDS = 7200.0
CLEANUP_RECEIPT_MARGIN_SECONDS = 600.0
TAR_MAX_SECONDS = 3600.0
REMOVAL_MAX_SECONDS = 5400.0
R2_MAX_SECONDS = 3600.0
ALERT_MAX_SECONDS = 30.0
PRIVILEGED_REMOVAL_HELPER = Path(
    "/usr/local/libexec/f12-privileged-candidate-removal.py"
)
EXPECTED_PRIVILEGED_REMOVAL_HELPER_SHA256 = (
    "2262bad48303613b733622b08e0524ab59be21f8f9aff3b6ee5f8055a33ab11d"
)
PRIVILEGED_REMOVAL_REQUEST_SCHEMA = "f12-privileged-candidate-removal-request-v1"
PRIVILEGED_REMOVAL_RESULT_SCHEMA = "f12-privileged-candidate-removal-result-v1"
PRIVILEGED_RELEASE_REFERENCE_ERROR_PREFIXES = (
    "SafetyError:source_path_named_inside_release:",
    "SafetyError:source_path_named_by_release_symlink:",
    "SafetyError:source_is_release_alias_target:",
)
CHILD_TERM_GRACE_SECONDS = 2.0
WORKER_POLL_SECONDS = 0.05
WORKER_SAFETY_REVALIDATE_SECONDS = 0.5
PROCESS_TREE_SCAN_MAX_PIDS = 65536
PROCESS_TREE_STABLE_EMPTY_SCANS = 3
PROCESS_TREE_STABLE_SCAN_SECONDS = 0.01
FREE_SPACE_GUARD_JOIN_SECONDS = 2.0
TERMINAL_RECEIPT_RESERVE_SECONDS = 30.0
FINAL_OUTER_SAFETY_SLACK_SECONDS = 10.0
TERMINAL_STDOUT_RESERVE_SECONDS = 3.0
TERMINAL_FALLBACK_RESERVE_SECONDS = 1.0
TERMINAL_STDOUT_MAX_BYTES = 1_000_000
CRON_BATCH_MEMBER_CAP = 1024
WORKER_FRAME_MAX_BYTES = 1_000_000
CHECKPOINT_MAX_AGE_SECONDS = 600.0
# A candidate may contain thousands of entries.  Process inventory is the one
# destructive-boundary proof that is safe to lease briefly across those
# entries: the immutable snapshot is still checked against the candidate at
# every approval, while lock, rollback, current authority and test references
# remain freshly validated per entry.  The candidate root and terminal result
# always bypass this lease.
DESTRUCTIVE_PROCESS_SNAPSHOT_MAX_AGE_SECONDS = 30.0
ACTIVE_TEST_SCAN_MAX_ROOTS = 64
ACTIVE_TEST_SCAN_MAX_ANCESTOR_DEPTH = 64
ACTIVE_TEST_SCAN_MAX_RECURSION_DEPTH = 24
ACTIVE_TEST_SCAN_MAX_DIRECTORIES = 4096
ACTIVE_TEST_SCAN_MAX_FILES = 10_000
ACTIVE_TEST_SCAN_MAX_TOTAL_BYTES = 128 * 1024 * 1024
ACTIVE_TEST_SCAN_MAX_FILE_BYTES = 8 * 1024 * 1024
ACTIVE_TEST_SCAN_MAX_SECONDS = 10.0
ACTIVE_TEST_SCAN_CHUNK_BYTES = 1024 * 1024
ROLLBACK_REVALIDATE_CHECKPOINTS = {
    "before_archive",
    "before_r2",
    "before_removal",
    "terminal_result",
}
ACTIVE_ROLLBACK_STATUSES = {
    "ACTIVE",
    "ARMED",
    "PASS_ACTIVE",
    "ROLLBACK_ACTIVE",
    "PASS_ROLLBACK_READY",
}



class SiteAuthorityError(RuntimeError):
    """The canonical site authority cannot safely authorize site discovery."""


class SourceDriftError(RuntimeError):
    """A source no longer matches its inspected/archive-time identity."""


class ArchivalAborted(Exception):
    pass


class ActiveTestFixtureReferenceProtected(ArchivalAborted):
    """A candidate is named by active test bytes or the scan failed closed."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = dict(evidence)
        reason = self.evidence.get("failure_reason") or self.evidence.get("token") or "unknown"
        super().__init__(f"ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED:{reason}")


class DeadlineExceeded(ArchivalAborted):
    """The bounded work window ended; cleanup and receipt time remains."""


class WorkerSupervisionError(ArchivalAborted):
    """A forked, session-isolated worker failed after bounded cleanup.

    ``events`` contains only complete progress frames received from the worker.
    Callers that can mutate sources must reconcile those frames against the
    descriptor-bound pre-removal manifest before describing effects.
    """

    def __init__(
        self,
        reason: str,
        *,
        stage: str,
        events: list[dict[str, Any]] | None = None,
        deadline_exhausted: bool = False,
        process_group: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.stage = stage
        self.events = list(events or [])
        self.deadline_exhausted = deadline_exhausted
        self.process_group = process_group


class PostUnlinkDurabilityError(RuntimeError):
    """An unlink succeeded but a later durability/cleanup step failed."""

    def __init__(
        self,
        *,
        path: Path,
        outcome: dict[str, Any],
        stage: str,
        cause: BaseException,
    ) -> None:
        super().__init__(
            f"post_unlink_durability_failed:{path}:{stage}:"
            f"{type(cause).__name__}:{cause}"
        )
        self.path = path
        self.outcome = dict(outcome)
        self.stage = stage
        self.cause = cause


@dataclass
class EffectLedger:
    """Append-only, in-memory truth for completed destructive syscalls.

    Removal events are committed before parent-directory fsync or any other
    fallible post-step.  Status events never rewrite the removal row.  The
    private key set prevents a completion frame and later reconciliation from
    counting the same syscall twice.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    uncertain_scopes: set[str] = field(default_factory=set)
    uncertainty_reasons: list[dict[str, str]] = field(default_factory=list)
    _removal_keys: set[tuple[str, str, str]] = field(default_factory=set)
    _allocated_groups_counted: set[tuple[int, int]] = field(default_factory=set)

    def record_removal(
        self,
        *,
        source_path: str,
        target_path: str,
        relative: str,
        operation: str,
        kind: str,
        logical_bytes: int,
        blocks: int,
        device: int,
        inode: int,
        nlink: int,
        object_removed: bool,
        provenance: str,
    ) -> bool:
        key = (str(source_path), str(relative), str(operation))
        if key in self._removal_keys:
            return False
        self._removal_keys.add(key)
        group_key = (int(device), int(inode))
        removed_links = 1 + sum(
            int(row.get("device", -1)) == group_key[0]
            and int(row.get("inode", -1)) == group_key[1]
            and row.get("event") == "removal_effect"
            for row in self.events
        )
        allocated_delta = 0
        if (
            group_key not in self._allocated_groups_counted
            and int(blocks) > 0
            and removed_links >= max(1, int(nlink))
        ):
            allocated_delta = int(blocks) * 512
            self._allocated_groups_counted.add(group_key)
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "event": "removal_effect",
                "source_path": str(source_path),
                "target_path": str(target_path),
                "relative": str(relative),
                "operation": str(operation),
                "kind": str(kind),
                "logical_bytes": max(0, int(logical_bytes)),
                "allocated_bytes_delta": allocated_delta,
                "blocks": int(blocks),
                "device": int(device),
                "inode": int(inode),
                "nlink": int(nlink),
                "object_removed": bool(object_removed),
                "provenance": provenance,
                "recorded_before_parent_fsync": True,
            }
        )
        return True

    def record_post_effect_status(
        self,
        *,
        source_path: str,
        relative: str,
        operation: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "sequence": len(self.events) + 1,
            "event": "post_effect_status",
            "source_path": source_path,
            "relative": relative,
            "operation": operation,
            "status": status,
        }
        if detail:
            row["detail"] = detail[:1000]
        self.events.append(row)

    def record_status(
        self,
        *,
        source_path: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        self.record_post_effect_status(
            source_path=source_path,
            relative="*",
            operation="scope",
            status=status,
            detail=detail,
        )

    def record_permission_event(
        self,
        *,
        source_path: str,
        frame: dict[str, Any],
        provenance: str | None = None,
    ) -> bool:
        """Append one descriptor-bound permission frame exactly once."""

        event = str(frame.get("event") or "")
        if event not in {
            "permission_adjustment_requested",
            "permission_adjustment_started",
            "permission_adjustment_completed",
            "permission_adjustment_restoration_started",
            "permission_adjustment_restoration_completed",
        }:
            return False
        relative = str(frame.get("relative") or ".")
        mode_before = str(frame.get("mode_before") or "")
        mode_after = str(frame.get("mode_after") or "")
        if any(
            row.get("event") == event
            and row.get("source_path") == source_path
            and row.get("relative") == relative
            and row.get("mode_before", "") == mode_before
            and row.get("mode_after", "") == mode_after
            for row in self.events
        ):
            return False
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "event": event,
                "source_path": source_path,
                "relative": relative,
                "operation": "fchmod_owner_write",
                "mode_before": mode_before,
                "mode_after": mode_after,
                "device": int(frame.get("device") or 0),
                "inode": int(frame.get("inode") or 0),
                "uid": int(frame.get("uid") or 0),
                "provenance": provenance
                or str(frame.get("provenance") or "worker_permission_frame"),
            }
        )
        return True

    def mark_uncertain(self, source_path: str, reason: str) -> None:
        self.uncertain_scopes.add(source_path)
        row = {"source_path": source_path, "reason": reason[:1000]}
        if row not in self.uncertainty_reasons:
            self.uncertainty_reasons.append(row)

    def removals_for(self, source_path: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.events
            if row.get("event") == "removal_effect"
            and row.get("source_path") == source_path
        ]

    def events_for(self, source_path: str) -> list[dict[str, Any]]:
        """Return the append-only event stream for one candidate scope."""

        return [
            row for row in self.events if row.get("source_path") == source_path
        ]

    def receipt(self) -> dict[str, Any]:
        removals = [row for row in self.events if row.get("event") == "removal_effect"]
        return {
            "schema": "f12-f13-in-memory-effect-ledger-v1",
            "append_only": True,
            "removal_effect_count": len(removals),
            "event_count": len(self.events),
            "effects_uncertain": bool(self.uncertain_scopes),
            "uncertain_scopes": sorted(self.uncertain_scopes),
            "uncertainty_reasons": list(self.uncertainty_reasons),
            "events": [dict(row) for row in self.events],
        }


class CommonLockContention(RuntimeError):
    """Another F12/F13 owner holds the shared interprocess lock."""


class CommonLockError(RuntimeError):
    """The shared lock could not be opened or validated safely."""


@dataclass
class RunDeadline:
    started_monotonic: float
    deadline_monotonic: float
    maximum_seconds: float
    exhausted_stage: str | None = None

    @classmethod
    def start(cls, seconds: float = INTERNAL_DEADLINE_MAX_SECONDS) -> "RunDeadline":
        bounded = min(float(seconds), INTERNAL_DEADLINE_MAX_SECONDS)
        if bounded <= 0:
            bounded = 0.0
        started = time.monotonic()
        return cls(started, started + bounded, bounded)

    def remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def check(self, stage: str) -> None:
        if self.remaining() <= 0:
            self.exhausted_stage = self.exhausted_stage or stage
            raise DeadlineExceeded(f"internal_deadline_exhausted:{stage}")

    def timeout(self, stage: str, cap_seconds: float) -> float:
        self.check(stage)
        value = min(float(cap_seconds), self.remaining())
        if value <= 0:
            self.exhausted_stage = self.exhausted_stage or stage
            raise DeadlineExceeded(f"internal_deadline_exhausted:{stage}")
        return value

    def cleanup_remaining(self, *, reserve_seconds: float = 0.0) -> float:
        """Return time left inside the outer scheduler envelope.

        Ordinary work is admitted only through ``remaining()`` (at most 6600
        seconds).  Cleanup, child reaping, reconciliation, and terminal receipt
        work may use the separately reserved outer margin, while still keeping
        a final receipt-print reserve.
        """

        outer_end = min(
            self.started_monotonic + OUTER_TIMEOUT_SECONDS,
            self.deadline_monotonic + CLEANUP_RECEIPT_MARGIN_SECONDS,
        ) - FINAL_OUTER_SAFETY_SLACK_SECONDS
        return max(0.0, outer_end - float(reserve_seconds) - time.monotonic())

    def cleanup_deadline(
        self,
        stage: str,
        *,
        reserve_seconds: float = TERMINAL_RECEIPT_RESERVE_SECONDS,
    ) -> "RunDeadline":
        remaining = self.cleanup_remaining(reserve_seconds=reserve_seconds)
        if remaining <= 0:
            raise DeadlineExceeded(f"cleanup_margin_exhausted:{stage}")
        now = time.monotonic()
        return RunDeadline(now, now + remaining, remaining)

    def terminal_deadline(self, stage: str) -> "RunDeadline":
        """Return the one hard terminal-I/O envelope below the scheduler kill."""

        remaining = self.cleanup_remaining(reserve_seconds=0.0)
        if remaining <= 0:
            raise DeadlineExceeded(f"terminal_margin_exhausted:{stage}")
        now = time.monotonic()
        return RunDeadline(now, now + remaining, remaining)

    def receipt(self) -> dict[str, Any]:
        return {
            "maximum_seconds": self.maximum_seconds,
            "outer_timeout_seconds": OUTER_TIMEOUT_SECONDS,
            "cleanup_receipt_margin_seconds": CLEANUP_RECEIPT_MARGIN_SECONDS,
            "final_outer_safety_slack_seconds": FINAL_OUTER_SAFETY_SLACK_SECONDS,
            "elapsed_seconds": round(time.monotonic() - self.started_monotonic, 6),
            "remaining_seconds": round(self.remaining(), 6),
            "exhausted_stage": self.exhausted_stage,
        }


def _check_deadline(deadline: RunDeadline | None, stage: str) -> None:
    if deadline is not None:
        deadline.check(stage)


def _call_deadline_aware(function: Any, *args: Any, deadline: RunDeadline) -> Any:
    """Keep focused monkeypatch/adaptor compatibility without hiding TypeErrors."""

    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    supports = any(
        parameter.name == "deadline"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if supports:
        return function(*args, deadline=deadline)
    return function(*args)


def _call_site_context(
    processes: Any,
    *,
    deadline: RunDeadline,
    rollback_index: "ActiveRollbackIndex | None",
) -> dict[str, Any]:
    """Call the live/fixture site-context function with supported safety args."""

    function = site_reference_context
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    has_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if has_kwargs or "deadline" in parameters:
        kwargs["deadline"] = deadline
    if rollback_index is not None and (has_kwargs or "rollback_index" in parameters):
        kwargs["rollback_index"] = rollback_index
    return function(processes, **kwargs)


def _call_process_inventory(
    *,
    deadline: RunDeadline | None = None,
    exclude_pids: set[int] | None = None,
) -> Any:
    function = process_inventory
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    has_kwargs = any(
        value.kind is inspect.Parameter.VAR_KEYWORD
        for value in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if deadline is not None and (has_kwargs or "deadline" in parameters):
        kwargs["deadline"] = deadline
    if exclude_pids is not None and (has_kwargs or "exclude_pids" in parameters):
        kwargs["exclude_pids"] = exclude_pids
    return function(**kwargs)


def _lock_file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_size,
    )


def _lock_parent_identity(value: os.stat_result) -> tuple[int, ...]:
    # Directory link counts legitimately change as receipt/archive subdirectories
    # are created.  The canonical parent binding is therefore pathname identity,
    # while the lock leaf itself binds owner, mode, links, and size in full.
    return (value.st_dev, value.st_ino)


def _lock_socket_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _canonical_absolute_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise CommonLockError(f"{label}_absolute_path_required:{path}")
    canonical = Path(os.path.normpath(str(path)))
    if canonical != path:
        raise CommonLockError(f"{label}_canonical_path_required:{path}")
    return canonical


def _assert_lock_path_components(path: Path) -> tuple[os.stat_result, os.stat_result]:
    """Reject every symlink component and return bound parent/leaf identities."""

    target = _canonical_absolute_path(path, label="common_lock")
    current = Path(target.anchor)
    for component in target.parts[1:-1]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise CommonLockError(
                f"common_lock_parent_unreadable:{current}:{type(exc).__name__}:{exc}"
            ) from exc
        if stat.S_ISLNK(value.st_mode):
            raise CommonLockError(f"common_lock_parent_symlink_forbidden:{current}")
        if not stat.S_ISDIR(value.st_mode):
            raise CommonLockError(f"common_lock_parent_directory_required:{current}")
    try:
        parent = target.parent.lstat()
        leaf = target.lstat()
    except OSError as exc:
        raise CommonLockError(
            f"common_lock_path_unreadable:{target}:{type(exc).__name__}:{exc}"
        ) from exc
    if stat.S_ISLNK(parent.st_mode):
        raise CommonLockError(f"common_lock_parent_symlink_forbidden:{target.parent}")
    if stat.S_ISLNK(leaf.st_mode):
        raise CommonLockError(f"common_lock_path_symlink_forbidden:{target}")
    if not stat.S_ISREG(leaf.st_mode):
        raise CommonLockError(f"common_lock_regular_file_required:{target}")
    return parent, leaf


def _fd_has_kernel_flock(
    descriptor: int, expected: tuple[int, ...], owner_pid: int
) -> bool:
    """Read Linux fdinfo, which binds a flock row to this open file description."""

    try:
        text = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
    except OSError:
        return False
    expected_device = expected[0]
    expected_inode = expected[1]
    expected_lock_id = (
        f"{os.major(expected_device):02x}:{os.minor(expected_device):02x}:{expected_inode}"
    )
    for line in text.splitlines():
        fields = line.split()
        if (
            len(fields) >= 7
            and fields[0] == "lock:"
            and fields[2] == "FLOCK"
            and fields[4] == "WRITE"
            and fields[5] == str(owner_pid)
            and fields[6].lower() == expected_lock_id.lower()
        ):
            return True
    return False


@dataclass
class _ActiveLockRecord:
    token: Any
    global_socket: socket.socket
    global_socket_descriptor: int
    global_socket_identity: tuple[int, ...]
    guard_descriptor: int
    parent_guard_descriptor: int
    path: Path
    file_identity: tuple[int, ...]
    parent_identity: tuple[int, ...]


def _assert_host_global_socket_owned(record: _ActiveLockRecord) -> None:
    descriptor = record.global_socket_descriptor
    if descriptor < 0 or record.global_socket.fileno() != descriptor:
        raise CommonLockError("common_lock_global_socket_descriptor_unavailable")
    try:
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise CommonLockError(
            f"common_lock_global_socket_descriptor_unreadable:{type(exc).__name__}:{exc}"
        ) from exc
    if (
        not stat.S_ISSOCK(descriptor_stat.st_mode)
        or _lock_socket_identity(descriptor_stat) != record.global_socket_identity
    ):
        raise CommonLockError("common_lock_global_socket_identity_drift")
    duplicate: socket.socket | None = None
    try:
        duplicate = socket.fromfd(descriptor, socket.AF_UNIX, socket.SOCK_STREAM)
        if duplicate.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise CommonLockError("common_lock_global_socket_type_drift")
        if hasattr(socket, "SO_DOMAIN") and duplicate.getsockopt(
            socket.SOL_SOCKET, socket.SO_DOMAIN
        ) != socket.AF_UNIX:
            raise CommonLockError("common_lock_global_socket_domain_drift")
        if duplicate.getsockname() != COMMON_LOCK_ABSTRACT_SOCKET_NAME:
            raise CommonLockError("common_lock_global_socket_name_drift")
    except CommonLockError:
        raise
    except OSError as exc:
        raise CommonLockError(
            f"common_lock_global_socket_validation_failed:{type(exc).__name__}:{exc}"
        ) from exc
    finally:
        if duplicate is not None:
            duplicate.close()


_ACTIVE_LOCK_TOKENS: dict[str, _ActiveLockRecord] = {}
_ACTIVE_LOCK_TOKENS_GUARD = threading.Lock()


@dataclass
class CommonLockToken:
    path: Path
    descriptor: int
    owner_pid: int
    acquired_monotonic: float
    released: bool = False
    nonce: str = field(default="", repr=False)
    bound_file_identity: tuple[int, ...] = field(default_factory=tuple, repr=False)
    bound_parent_identity: tuple[int, ...] = field(default_factory=tuple, repr=False)
    _cleanup_callbacks: list[Any] = field(default_factory=list, repr=False)

    def _record(self) -> _ActiveLockRecord:
        with _ACTIVE_LOCK_TOKENS_GUARD:
            record = _ACTIVE_LOCK_TOKENS.get(self.nonce)
        if record is None or record.token is not self:
            raise CommonLockError("common_lock_token_not_factory_authenticated")
        return record

    def assert_owned(self, *, allow_inherited_worker: bool = False) -> None:
        current_pid = os.getpid()
        inherited_worker = bool(
            allow_inherited_worker
            and current_pid != self.owner_pid
            and os.getppid() == self.owner_pid
        )
        if (
            self.released
            or (self.owner_pid != current_pid and not inherited_worker)
            or self.descriptor < 0
        ):
            raise CommonLockError("common_lock_token_not_owned")
        record = self._record()
        if record.path != self.path:
            raise CommonLockError("common_lock_token_path_drift")
        _assert_host_global_socket_owned(record)
        try:
            descriptor_stat = os.fstat(self.descriptor)
            guard_stat = os.fstat(record.guard_descriptor)
            parent_guard_stat = os.fstat(record.parent_guard_descriptor)
        except OSError as exc:
            raise CommonLockError(
                f"common_lock_descriptor_unreadable:{type(exc).__name__}:{exc}"
            ) from exc
        descriptor_identity = _lock_file_identity(descriptor_stat)
        guard_identity = _lock_file_identity(guard_stat)
        if descriptor_identity[:2] != self.bound_file_identity[:2]:
            raise CommonLockError("common_lock_descriptor_identity_drift")
        if descriptor_identity[2:4] != self.bound_file_identity[2:4]:
            raise CommonLockError("common_lock_owner_drift")
        if descriptor_identity[4] != self.bound_file_identity[4]:
            raise CommonLockError("common_lock_mode_drift")
        if descriptor_identity[5] != self.bound_file_identity[5]:
            raise CommonLockError("common_lock_link_count_drift")
        if descriptor_identity[6] != self.bound_file_identity[6]:
            raise CommonLockError("common_lock_size_drift")
        if guard_identity[:2] != self.bound_file_identity[:2]:
            raise CommonLockError("common_lock_guard_descriptor_identity_drift")
        if guard_identity != self.bound_file_identity:
            raise CommonLockError("common_lock_guard_metadata_drift")
        if _lock_parent_identity(parent_guard_stat) != self.bound_parent_identity:
            raise CommonLockError("common_lock_parent_guard_identity_drift")
        try:
            parent_stat, path_stat = _assert_lock_path_components(self.path)
        except CommonLockError:
            raise
        path_identity = _lock_file_identity(path_stat)
        parent_identity = _lock_parent_identity(parent_stat)
        if (path_stat.st_dev, path_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise CommonLockError("common_lock_pathname_identity_drift")
        if path_identity[2:4] != self.bound_file_identity[2:4]:
            raise CommonLockError("common_lock_owner_drift")
        if path_identity[4] != self.bound_file_identity[4]:
            raise CommonLockError("common_lock_mode_drift")
        if path_identity[5] != self.bound_file_identity[5]:
            raise CommonLockError("common_lock_link_count_drift")
        if path_identity != self.bound_file_identity:
            raise CommonLockError("common_lock_path_identity_drift")
        if parent_identity != self.bound_parent_identity:
            raise CommonLockError("common_lock_parent_identity_drift")
        if not _fd_has_kernel_flock(
            self.descriptor, self.bound_file_identity, self.owner_pid
        ) or not _fd_has_kernel_flock(
            record.guard_descriptor, self.bound_file_identity, self.owner_pid
        ) or not _fd_has_kernel_flock(
            record.parent_guard_descriptor, self.bound_parent_identity, self.owner_pid
        ):
            raise CommonLockError("common_lock_kernel_flock_not_owned")

    def register_cleanup(self, callback: Any) -> None:
        self.assert_owned()
        if callback not in self._cleanup_callbacks:
            self._cleanup_callbacks.append(callback)

    def release(self) -> None:
        if self.released:
            return
        record = self._record()
        ownership_error: BaseException | None = None
        try:
            self.assert_owned()
        except BaseException as exc:
            ownership_error = exc
        for callback in reversed(self._cleanup_callbacks):
            try:
                callback()
            except BaseException as exc:
                ownership_error = ownership_error or exc
        self._cleanup_callbacks.clear()
        with _ACTIVE_LOCK_TOKENS_GUARD:
            if _ACTIVE_LOCK_TOKENS.get(self.nonce) is record:
                del _ACTIVE_LOCK_TOKENS[self.nonce]
        global_socket_matches = False
        try:
            global_socket_matches = (
                record.global_socket.fileno() == record.global_socket_descriptor
                and _lock_socket_identity(os.fstat(record.global_socket_descriptor))
                == record.global_socket_identity
            )
        except OSError:
            pass
        descriptor_matches = False
        try:
            descriptor_matches = (
                _lock_file_identity(os.fstat(self.descriptor))
                == self.bound_file_identity
            )
        except OSError:
            pass
        for held_descriptor in (
            record.guard_descriptor,
            record.parent_guard_descriptor,
        ):
            try:
                fcntl.flock(held_descriptor, fcntl.LOCK_UN)
            except OSError as exc:
                ownership_error = ownership_error or exc
        for held_descriptor in (
            record.guard_descriptor,
            record.parent_guard_descriptor,
        ):
            try:
                os.close(held_descriptor)
            except OSError as exc:
                ownership_error = ownership_error or exc
        if descriptor_matches:
            try:
                os.close(self.descriptor)
            except OSError as exc:
                ownership_error = ownership_error or exc
        if global_socket_matches:
            try:
                record.global_socket.close()
            except OSError as exc:
                ownership_error = ownership_error or exc
        else:
            try:
                record.global_socket.detach()
            except OSError:
                pass
            ownership_error = ownership_error or CommonLockError(
                "common_lock_global_socket_identity_drift"
            )
        self.descriptor = -1
        self.released = True
        if ownership_error is not None:
            raise CommonLockError(str(ownership_error)) from ownership_error

    def __enter__(self) -> "CommonLockToken":
        self.assert_owned()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def acquire_common_lock(path: Path | None = None) -> CommonLockToken:
    """Acquire the host-global socket first, then descriptor-bound file locks."""

    target = _canonical_absolute_path(Path(path or LOCK_PATH), label="common_lock")
    global_socket: socket.socket | None = None
    try:
        global_socket = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        global_socket.set_inheritable(False)
        global_socket.bind(COMMON_LOCK_ABSTRACT_SOCKET_NAME)
    except OSError as exc:
        if global_socket is not None:
            global_socket.close()
        if exc.errno == errno.EADDRINUSE:
            raise CommonLockContention("common_lock_host_global_socket_contended") from exc
        raise CommonLockError(
            f"common_lock_host_global_socket_failed:{type(exc).__name__}:{exc}"
        ) from exc

    descriptor = -1
    guard_descriptor = -1
    parent_guard_descriptor = -1
    inserted_nonce: str | None = None
    try:
        with _ACTIVE_LOCK_TOKENS_GUARD:
            if _ACTIVE_LOCK_TOKENS:
                raise CommonLockContention("common_lock_contended_active_token")
        parent_stat, path_stat = _assert_lock_path_components(target)
        expected = _lock_file_identity(path_stat)
        if path_stat.st_uid != os.geteuid() or path_stat.st_gid != os.getegid():
            raise CommonLockError(f"common_lock_owner_invalid:{target}")
        lock_mode = stat.S_IMODE(path_stat.st_mode)
        if lock_mode & 0o022 or lock_mode & 0o600 != 0o600:
            raise CommonLockError(
                f"common_lock_mode_invalid:{target}:{lock_mode:04o}:"
                "owner_rw_and_no_group_world_write_required"
            )
        if path_stat.st_nlink != 1:
            raise CommonLockError(f"common_lock_link_count_invalid:{target}")
        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise CommonLockError(
                f"common_lock_open_failed:{target}:{type(exc).__name__}:{exc}"
            ) from exc
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _lock_file_identity(opened) != expected:
            raise CommonLockError(f"common_lock_open_identity_drift:{target}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommonLockContention(f"common_lock_contended:{target}") from exc
        guard_descriptor = os.dup(descriptor)
        os.set_inheritable(guard_descriptor, False)
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_guard_descriptor = os.open(target.parent, parent_flags)
        if _lock_parent_identity(os.fstat(parent_guard_descriptor)) != _lock_parent_identity(
            parent_stat
        ):
            raise CommonLockError(f"common_lock_parent_open_identity_drift:{target}")
        try:
            fcntl.flock(parent_guard_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommonLockContention(f"common_lock_parent_contended:{target.parent}") from exc
        parent_after, path_after = _assert_lock_path_components(target)
        if _lock_file_identity(path_after) != expected:
            raise CommonLockError(f"common_lock_pathname_identity_drift:{target}")
        parent_identity = _lock_parent_identity(parent_stat)
        if _lock_parent_identity(parent_after) != parent_identity:
            raise CommonLockError(f"common_lock_parent_identity_drift:{target}")
        nonce = secrets.token_hex(32)
        token = CommonLockToken(
            target,
            descriptor,
            os.getpid(),
            time.monotonic(),
            nonce=nonce,
            bound_file_identity=expected,
            bound_parent_identity=parent_identity,
        )
        record = _ActiveLockRecord(
            token,
            global_socket,
            global_socket.fileno(),
            _lock_socket_identity(os.fstat(global_socket.fileno())),
            guard_descriptor,
            parent_guard_descriptor,
            target,
            expected,
            parent_identity,
        )
        with _ACTIVE_LOCK_TOKENS_GUARD:
            if any(existing.path == target for existing in _ACTIVE_LOCK_TOKENS.values()):
                raise CommonLockContention(f"common_lock_contended_active_token:{target}")
            _ACTIVE_LOCK_TOKENS[nonce] = record
            inserted_nonce = nonce
        token.assert_owned()
        return token
    except BaseException:
        if inserted_nonce is not None:
            with _ACTIVE_LOCK_TOKENS_GUARD:
                existing = _ACTIVE_LOCK_TOKENS.get(inserted_nonce)
                if existing is not None and existing.token is token:
                    del _ACTIVE_LOCK_TOKENS[inserted_nonce]
        if guard_descriptor >= 0:
            try:
                os.close(guard_descriptor)
            except OSError:
                pass
        if parent_guard_descriptor >= 0:
            try:
                fcntl.flock(parent_guard_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(parent_guard_descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass
        if global_socket is not None:
            global_socket.close()
        raise


@dataclass
class ProcessReferenceSnapshot:
    rows: list[dict[str, Any]]
    references: tuple[str, ...]
    captured_monotonic: float
    proc_pids_seen: int
    fd_links_read: int
    maps_files_read: int
    maps_lines_read: int
    shallow_references: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def receipt(self) -> dict[str, Any]:
        return {
            "process_rows": len(self.rows),
            "unique_references": len(self.references),
            "unique_shallow_references": len(self.shallow_references),
            "proc_pids_seen": self.proc_pids_seen,
            "fd_links_read": self.fd_links_read,
            "maps_files_read": self.maps_files_read,
            "maps_lines_read": self.maps_lines_read,
        }


@dataclass
class ActiveRollbackIndex:
    refs: set[Path]
    json_file_count: int = 0
    json_total_bytes: int = 0
    relevant_file_count: int = 0
    relevant_total_bytes: int = 0
    parsed_file_count: int = 0
    scan_elapsed_seconds: float = 0.0
    registry_path: Path | None = None
    registry_descriptor: int = -1
    registry_identity: tuple[int, ...] = field(default_factory=tuple, repr=False)
    registry_sha256: str | None = None
    generation: str | None = None
    immutable_version: int | None = None
    entries: tuple[dict[str, Any], ...] = ()
    bootstrap_provenance: dict[str, Any] = field(default_factory=dict)
    recent_refs: set[Path] = field(default_factory=set)
    active_refs: set[Path] = field(default_factory=set)
    recent_entries: tuple[dict[str, Any], ...] = ()
    evidence_path: Path | None = None
    evidence_descriptor: int = -1
    evidence_identity: tuple[int, ...] = field(default_factory=tuple, repr=False)
    evidence_sha256: str | None = None
    evidence_schema: str | None = None
    entry_set_digest_sha256: str | None = None
    inventory_digest_sha256: str | None = None
    current_entry_counts: dict[str, int] = field(default_factory=dict)
    validation_count: int = 0
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        if self.registry_descriptor >= 0:
            try:
                os.close(self.registry_descriptor)
            except OSError:
                pass
            self.registry_descriptor = -1
        if self.evidence_descriptor >= 0:
            try:
                os.close(self.evidence_descriptor)
            except OSError:
                pass
            self.evidence_descriptor = -1
        self.closed = True

    def receipt(self) -> dict[str, Any]:
        return {
            "status": "VALID",
            "schema": ROLLBACK_REGISTRY_SCHEMA,
            "immutable_version": self.immutable_version,
            "generation": self.generation,
            "registry_path": str(self.registry_path) if self.registry_path else None,
            "registry_sha256": self.registry_sha256,
            "registry_identity": {
                "device": self.registry_identity[0],
                "inode": self.registry_identity[1],
                "uid": self.registry_identity[2],
                "gid": self.registry_identity[3],
                "mode": format(self.registry_identity[4], "04o"),
                "nlink": self.registry_identity[5],
                "size_bytes": self.registry_identity[6],
                "mtime_ns": self.registry_identity[7],
            }
            if len(self.registry_identity) == 8
            else None,
            "entry_count": len(self.entries) + len(self.recent_entries),
            "active_rollback_entry_count": len(self.entries),
            "recent_reference_entry_count": len(self.recent_entries),
            "ref_count": len(self.refs),
            "active_rollback_ref_count": len(self.active_refs),
            "recent_reference_ref_count": len(self.recent_refs),
            "load_count": 1 if self.registry_descriptor >= 0 or self.closed else 0,
            "validation_count": self.validation_count,
            "load_elapsed_seconds": round(self.scan_elapsed_seconds, 6),
            "historical_bootstrap_provenance": dict(self.bootstrap_provenance),
            "current_entry_counts": dict(self.current_entry_counts),
            "entry_set_digest_sha256": self.entry_set_digest_sha256,
            "inventory_digest_sha256": self.inventory_digest_sha256,
            "generation_evidence": {
                "path": str(self.evidence_path) if self.evidence_path else None,
                "sha256": self.evidence_sha256,
                "schema": self.evidence_schema,
                "identity": {
                    "device": self.evidence_identity[0],
                    "inode": self.evidence_identity[1],
                    "uid": self.evidence_identity[2],
                    "gid": self.evidence_identity[3],
                    "mode": format(self.evidence_identity[4], "04o"),
                    "nlink": self.evidence_identity[5],
                    "size_bytes": self.evidence_identity[6],
                    "mtime_ns": self.evidence_identity[7],
                }
                if len(self.evidence_identity) == 8
                else None,
            },
            "admission": {
                "method": "explicit_evidence_bound_registry_membership_only",
                "filename_admission": False,
                "full_reports_corpus_walks": 0,
                "automatic_runtime_discovery_allowed": False,
                "mtime_expiration": False,
                "active_rollback_mtime_expiration": False,
                "recent_reference_expiry_policy": RECENT_REFERENCE_EXPIRY_POLICY,
            },
            "limits": {
                "max_registry_bytes": ROLLBACK_REGISTRY_MAX_BYTES,
                "max_entries": ROLLBACK_REGISTRY_MAX_ENTRIES,
                "max_roots_per_entry": ROLLBACK_REGISTRY_MAX_ROOTS_PER_ENTRY,
                "max_entry_bytes": ROLLBACK_AUTHORITY_ENTRY_MAX_BYTES,
            },
        }


@dataclass
class SafetyCheckpoint:
    name: str
    processes: ProcessReferenceSnapshot | list[dict[str, Any]]
    site_context: dict[str, Any] | None
    created_monotonic: float = field(default_factory=time.monotonic)


@dataclass
class RunContext:
    lock: CommonLockToken
    deadline: RunDeadline
    checkpoints: dict[str, SafetyCheckpoint] = field(default_factory=dict)
    effects: dict[str, int] = field(default_factory=lambda: _zero_effects())
    effect_ledger: EffectLedger = field(default_factory=EffectLedger)
    child_groups_started: int = 0
    child_groups_terminated: int = 0
    child_groups_killed: int = 0
    child_groups_reaped: int = 0
    child_trees_started: int = 0
    child_tree_scans: int = 0
    child_tree_pidfds_opened: int = 0
    child_tree_descendants_discovered: int = 0
    child_tree_detached_discovered: int = 0
    child_tree_term_signals: int = 0
    child_tree_kill_signals: int = 0
    child_tree_descendants_reaped: int = 0
    child_tree_stable_empty_proofs: int = 0
    child_tree_identity_failures: int = 0
    child_tree_root_bind_failures: int = 0
    child_tree_cleanup_root_bindings: int = 0
    free_space_guards_started: int = 0
    free_space_guards_stopped: int = 0
    free_space_guards_joined: int = 0
    free_space_guards_survived: int = 0
    free_space_guards_forced_killed: int = 0
    process_snapshots_taken: int = 0
    cron_member_reference_refreshes: int = 0
    active_rollback_index: ActiveRollbackIndex | None = None
    active_rollback_index_build_count: int = 0
    active_rollback_index_validation_count: int = 0
    checkpoint_refresh_counts: dict[str, int] = field(default_factory=dict)
    last_site_context: dict[str, Any] | None = None
    destructive_process_checkpoint: SafetyCheckpoint | None = None
    destructive_process_checkpoint_key: tuple[str, tuple[int, ...]] | None = None
    destructive_process_snapshot_reuse_count: int = 0
    destructive_active_test_scan: ActiveTestReferenceScan | None = None
    destructive_active_test_scan_key: str | None = None
    destructive_active_test_scan_reuse_count: int = 0
    destructive_active_test_scan_refresh_count: int = 0

    def assert_owned(self) -> None:
        self.lock.assert_owned()
        self.deadline.check("run_context_ownership")

    def checkpoint(
        self, name: str, *, require_site_context: bool = False
    ) -> SafetyCheckpoint:
        self.assert_owned()
        cached = self.checkpoints.get(name)
        if cached is not None:
            age = time.monotonic() - cached.created_monotonic
            if age <= CHECKPOINT_MAX_AGE_SECONDS:
                if (
                    name in ROLLBACK_REVALIDATE_CHECKPOINTS
                    and self.active_rollback_index is not None
                    and self.active_rollback_index.registry_descriptor >= 0
                ):
                    validate_active_rollback_index(
                        self.active_rollback_index, deadline=self.deadline
                    )
                    self.active_rollback_index_validation_count += 1
                    if cached.site_context is not None:
                        cached.site_context["active_rollback_index_receipt"] = (
                            self.active_rollback_index.receipt()
                        )
                # Validation itself may cross the age boundary. Never return a
                # named snapshot that is stale at the point of use.
                age = time.monotonic() - cached.created_monotonic
                if age <= CHECKPOINT_MAX_AGE_SECONDS:
                    if require_site_context and cached.site_context is None:
                        cached.site_context = self._capture_site_context(cached.processes)
                        if (
                            time.monotonic() - cached.created_monotonic
                            <= CHECKPOINT_MAX_AGE_SECONDS
                        ):
                            return cached
                    else:
                        return cached

            # Refresh stale process/authority context atomically. The canonical
            # rollback index remains descriptor-bound and is never rebuilt or
            # rediscovered from the reports corpus during a refresh.
            self.deadline.check(f"checkpoint:{name}:stale_refresh_process_snapshot")
            processes = _call_deadline_aware(
                process_inventory, deadline=self.deadline
            )
            self.process_snapshots_taken += 1
            needs_site_context = bool(
                require_site_context or cached.site_context is not None
            )
            site_context: dict[str, Any] | None = None
            if needs_site_context:
                site_context = self._capture_site_context(processes)
                if not site_context.get("site_authority_valid", False):
                    raise ArchivalAborted(
                        f"checkpoint_refresh_authority_invalid:{name}:"
                        f"{site_context.get('site_authority_error') or 'unknown'}"
                    )
            elif (
                name in ROLLBACK_REVALIDATE_CHECKPOINTS
                and self.active_rollback_index is not None
                and self.active_rollback_index.registry_descriptor >= 0
            ):
                validate_active_rollback_index(
                    self.active_rollback_index, deadline=self.deadline
                )
                self.active_rollback_index_validation_count += 1
            self.assert_owned()
            refreshed = SafetyCheckpoint(name, processes, site_context)
            self.checkpoints[name] = refreshed
            self.checkpoint_refresh_counts[name] = (
                self.checkpoint_refresh_counts.get(name, 0) + 1
            )
            return refreshed
        self.deadline.check(f"checkpoint:{name}:process_snapshot")
        processes = _call_deadline_aware(
            process_inventory, deadline=self.deadline
        )
        self.process_snapshots_taken += 1
        site_context = self._capture_site_context(processes) if require_site_context else None
        checkpoint = SafetyCheckpoint(name, processes, site_context)
        self.checkpoints[name] = checkpoint
        return checkpoint

    def _capture_site_context(
        self, processes: ProcessReferenceSnapshot | list[dict[str, Any]]
    ) -> dict[str, Any]:
        existing = self.active_rollback_index
        if existing is None:
            self.active_rollback_index_build_count += 1
        else:
            self.active_rollback_index_validation_count += 1
        context = _call_site_context(
            processes,
            deadline=self.deadline,
            rollback_index=existing,
        )
        returned = context.get("active_rollback_index")
        if existing is None and isinstance(returned, ActiveRollbackIndex):
            self.active_rollback_index = returned
            if returned.registry_descriptor >= 0:
                self.lock.register_cleanup(returned.close)
        self.last_site_context = context
        return context

    def terminal_checkpoint(self) -> SafetyCheckpoint:
        """Fresh terminal validation under the reserved outer cleanup margin."""

        self.lock.assert_owned()
        terminal_deadline = (
            self.deadline.cleanup_deadline("terminal_checkpoint")
            if hasattr(self.deadline, "cleanup_deadline")
            else self.deadline
        )
        terminal_deadline.check("checkpoint:terminal_result:process_snapshot")
        processes = _call_deadline_aware(
            process_inventory,
            deadline=terminal_deadline,
        )
        self.process_snapshots_taken += 1
        site_context: dict[str, Any] | None = None
        if self.active_rollback_index is not None:
            self.active_rollback_index_validation_count += 1
            site_context = _call_site_context(
                processes,
                deadline=terminal_deadline,
                rollback_index=self.active_rollback_index,
            )
        checkpoint = SafetyCheckpoint("terminal_result", processes, site_context)
        self.checkpoints["terminal_result"] = checkpoint
        if (
            self.active_rollback_index is not None
            and (
                checkpoint.site_context is None
                or not checkpoint.site_context.get("site_authority_valid", False)
            )
        ):
            reason = (
                checkpoint.site_context.get("site_authority_error")
                if checkpoint.site_context is not None
                else "terminal_site_context_missing"
            )
            raise ArchivalAborted(f"terminal_authority_invalid:{reason}")
        return checkpoint

    def destructive_reference_boundary(
        self,
        source: Path,
        *,
        deadline: RunDeadline | None = None,
        exclude_pids: set[int] | None = None,
        reuse_process_snapshot: bool = False,
        force_process_snapshot: bool = False,
    ) -> SafetyCheckpoint:
        """Process/reference/authority proof immediately before mutation.

        Only directory-member approvals opt into the monotonic, candidate-bound
        process snapshot lease.  Every call still validates the common lock,
        rollback registry, current site authority and active test references.
        Callers outside that narrow loop retain the historical fresh-snapshot
        behavior, and the final candidate-root approval forces a fresh scan.
        """

        active_deadline = deadline or self.deadline
        self.assert_worker_boundary(deadline=active_deadline)
        excluded = set(exclude_pids or set())
        cache_key = (
            str(source.resolve(strict=False)),
            tuple(sorted(excluded)),
        )
        checkpoint: SafetyCheckpoint | None = None
        if (
            reuse_process_snapshot
            and not force_process_snapshot
            and self.destructive_process_checkpoint_key == cache_key
            and self.destructive_process_checkpoint is not None
            and time.monotonic()
            - self.destructive_process_checkpoint.created_monotonic
            <= DESTRUCTIVE_PROCESS_SNAPSHOT_MAX_AGE_SECONDS
        ):
            checkpoint = self.destructive_process_checkpoint
            self.destructive_process_snapshot_reuse_count += 1
        if checkpoint is None:
            active_deadline.check("destructive_boundary:before_process_snapshot")
            processes = _call_process_inventory(
                deadline=active_deadline,
                exclude_pids=excluded,
            )
            self.process_snapshots_taken += 1
            checkpoint = SafetyCheckpoint(
                name="destructive_boundary",
                processes=processes,
                site_context=None,
                created_monotonic=time.monotonic(),
            )
            if reuse_process_snapshot:
                self.destructive_process_checkpoint = checkpoint
                self.destructive_process_checkpoint_key = cache_key
        processes = checkpoint.processes
        if path_is_active(source, processes, deep=True):
            raise SourceDriftError(f"source_became_process_active:{source}")
        release_refs: set[Path] = set()
        if under(source, FLAGSHIP):
            release_refs = release_symlink_target_references(
                deadline=active_deadline
            )
        site_context: dict[str, Any] | None = None
        if self.active_rollback_index is not None:
            authority = load_site_tree_authority()
            if authority.get("status") != "VALID":
                raise SiteAuthorityError(
                    f"site_authority_invalid_at_destructive_boundary:{source}:"
                    f"{authority.get('status')}"
                )
            refs = set(authority.get("refs", set()))
            refs.update(self.active_rollback_index.refs)
            refs.update(release_refs)
            site_context = dict(self.last_site_context or {})
            site_context.update(
                {
                    "site_authority_valid": True,
                    "site_authority_error": None,
                    "authority": authority,
                    "refs": refs,
                    "active_rollback_index": self.active_rollback_index,
                    "active_rollback_index_receipt": self.active_rollback_index.receipt(),
                }
            )
            if _reference_protects(source, refs):
                raise SourceDriftError(f"source_became_authority_referenced:{source}")
        elif _reference_protects(source, release_refs):
            raise SourceDriftError(f"source_became_release_referenced:{source}")
        active_test_scan: ActiveTestReferenceScan | None = None
        reusing_active_test_scan = False
        if reuse_process_snapshot:
            active_test_key = str(source.resolve(strict=False))
            if (
                self.destructive_active_test_scan is None
                or self.destructive_active_test_scan_key != active_test_key
            ):
                # Directory members share immutable test bytes for exactly one
                # candidate.  ActiveTestReferenceScan still revalidates every
                # cached directory and file identity before every effect, so a
                # concurrent test write remains a fail-closed drift event.
                self.destructive_active_test_scan = ActiveTestReferenceScan()
                self.destructive_active_test_scan_key = active_test_key
            else:
                self.destructive_active_test_scan_reuse_count += 1
                reusing_active_test_scan = True
            active_test_scan = self.destructive_active_test_scan
        active_test_evidence = active_test_reference_gate(
            source,
            scan=active_test_scan,
        )
        if (
            reusing_active_test_scan
            and active_test_scan is not None
            and active_test_evidence.get("protected") is True
            and active_test_scan.refreshable_identity_drift(active_test_evidence)
        ):
            # A long per-entry removal can overlap an ordinary test-file write.
            # Accept no effect from the stale cache: replace it only with a
            # complete fresh scan that independently fails closed on references,
            # inode replacement, symlinks, special files, bounds, or read drift.
            refreshed_active_test_scan = ActiveTestReferenceScan()
            active_test_evidence = active_test_reference_gate(
                source,
                scan=refreshed_active_test_scan,
            )
            self.destructive_active_test_scan = refreshed_active_test_scan
            self.destructive_active_test_scan_refresh_count += 1
        if active_test_evidence.get("protected") is True:
            raise ActiveTestFixtureReferenceProtected(active_test_evidence)
        active_deadline.check("destructive_boundary:after_authority")
        # Per-entry authority work can itself outlive a reused process lease.
        # Refresh and repeat the complete boundary rather than approving from a
        # snapshot that became stale while the other independent gates ran.
        if (
            reuse_process_snapshot
            and not force_process_snapshot
            and time.monotonic() - checkpoint.created_monotonic
            > DESTRUCTIVE_PROCESS_SNAPSHOT_MAX_AGE_SECONDS
        ):
            return self.destructive_reference_boundary(
                source,
                deadline=active_deadline,
                exclude_pids=excluded,
                reuse_process_snapshot=True,
                force_process_snapshot=True,
            )
        checkpoint.site_context = site_context
        return checkpoint

    def cron_member_reference_boundary(
        self,
        source: Path,
        *,
        deadline: RunDeadline,
        exclude_pids: set[int] | None = None,
    ) -> SafetyCheckpoint:
        """Refresh current process/reference state for exactly one cron unlink."""

        self.cron_member_reference_refreshes += 1
        checkpoint = self.destructive_reference_boundary(
            source,
            deadline=deadline,
            exclude_pids=exclude_pids,
        )
        return checkpoint

    def record_removal_effect(
        self,
        *,
        source_path: str,
        target_path: str,
        relative: str,
        operation: str,
        kind: str,
        logical_bytes: int,
        blocks: int,
        device: int,
        inode: int,
        nlink: int,
        object_removed: bool,
        provenance: str,
    ) -> bool:
        added = self.effect_ledger.record_removal(
            source_path=source_path,
            target_path=target_path,
            relative=relative,
            operation=operation,
            kind=kind,
            logical_bytes=logical_bytes,
            blocks=blocks,
            device=device,
            inode=inode,
            nlink=nlink,
            object_removed=object_removed,
            provenance=provenance,
        )
        if not added:
            return False
        if kind == "regular_file":
            self.effects["source_files_removed"] += 1
        elif kind == "directory":
            self.effects["source_directories_removed"] += 1
        if object_removed:
            self.effects["source_objects_removed"] += 1
        return True

    def mark_effects_uncertain(self, source_path: str, reason: str) -> None:
        self.effect_ledger.mark_uncertain(source_path, reason)

    def assert_terminal_integrity(self) -> None:
        self.lock.assert_owned()
        if self.active_rollback_index is not None:
            cleanup_deadline = self.deadline.cleanup_deadline(
                "terminal_integrity",
                reserve_seconds=0.0,
            )
            validate_active_rollback_index(
                self.active_rollback_index, deadline=cleanup_deadline
            )
            self.active_rollback_index_validation_count += 1

    def assert_worker_boundary(self, *, deadline: RunDeadline | None = None) -> None:
        """Validate lock and canonical registry without rebuilding authority."""

        self.lock.assert_owned()
        if self.active_rollback_index is not None:
            validate_active_rollback_index(
                self.active_rollback_index,
                deadline=deadline,
            )
            self.active_rollback_index_validation_count += 1

    def runtime_receipt(self) -> dict[str, Any]:
        return {
            "deadline": self.deadline.receipt(),
            "checkpoint_names": sorted(self.checkpoints),
            "checkpoint_refresh_counts": dict(sorted(self.checkpoint_refresh_counts.items())),
            "checkpoint_refresh_count": sum(self.checkpoint_refresh_counts.values()),
            "process_snapshot_count": self.process_snapshots_taken,
            "destructive_process_snapshot_reuse_count": (
                self.destructive_process_snapshot_reuse_count
            ),
            "destructive_active_test_scan_reuse_count": (
                self.destructive_active_test_scan_reuse_count
            ),
            "destructive_active_test_scan_refresh_count": (
                self.destructive_active_test_scan_refresh_count
            ),
            "destructive_process_snapshot_max_age_seconds": (
                DESTRUCTIVE_PROCESS_SNAPSHOT_MAX_AGE_SECONDS
            ),
            "cron_member_reference_refresh_count": self.cron_member_reference_refreshes,
            "active_rollback_scan_count": self.active_rollback_index_build_count,
            "active_rollback_index_validation_count": (
                self.active_rollback_index_validation_count
            ),
            "rollback_registry_load_count": self.active_rollback_index_build_count,
            "rollback_registry_validation_count": (
                self.active_rollback_index_validation_count
            ),
            "child_process_groups": {
                "started": self.child_groups_started,
                "terminated": self.child_groups_terminated,
                "killed": self.child_groups_killed,
                "reaped": self.child_groups_reaped,
            },
            "child_process_trees": {
                "started": self.child_trees_started,
                "proc_scans": self.child_tree_scans,
                "pidfds_opened": self.child_tree_pidfds_opened,
                "descendants_discovered": self.child_tree_descendants_discovered,
                "detached_descendants_discovered": (
                    self.child_tree_detached_discovered
                ),
                "term_signals": self.child_tree_term_signals,
                "kill_signals": self.child_tree_kill_signals,
                "descendants_reaped": self.child_tree_descendants_reaped,
                "stable_empty_proofs": self.child_tree_stable_empty_proofs,
                "identity_failures": self.child_tree_identity_failures,
                "root_bind_failures": self.child_tree_root_bind_failures,
                "cleanup_root_bindings": self.child_tree_cleanup_root_bindings,
            },
            "free_space_guards": {
                "started": self.free_space_guards_started,
                "stopped": self.free_space_guards_stopped,
                "joined": self.free_space_guards_joined,
                "survived_join": self.free_space_guards_survived,
            },
            "free_space_guard_forced_killed": self.free_space_guards_forced_killed,
            "effect_ledger": {
                "removal_effect_count": sum(
                    row.get("event") == "removal_effect"
                    for row in self.effect_ledger.events
                ),
                "effects_uncertain": bool(self.effect_ledger.uncertain_scopes),
                "uncertain_scope_count": len(self.effect_ledger.uncertain_scopes),
            },
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def _active_test_scan_hook(_stage: str, _path: Path) -> None:
    """Provider-free hostile-test seam; production intentionally does nothing."""


def _active_test_boundary_hook(_stage: str, _candidate: Path) -> None:
    """Provider-free boundary seam; production intentionally does nothing."""


class _ActiveTestScanFailure(RuntimeError):
    def __init__(self, reason: str, path: Path | None = None) -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason if path is None else f"{reason}:{path}")


def _active_test_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _lexically_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _active_test_candidate_path(candidate: Path) -> Path:
    if not candidate.is_absolute() or Path(os.path.normpath(str(candidate))) != candidate:
        raise _ActiveTestScanFailure("CANDIDATE_NONCANONICAL", candidate)
    try:
        resolved = candidate.resolve(strict=True)
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise _ActiveTestScanFailure(
            f"CANDIDATE_UNREADABLE:{type(exc).__name__}", candidate
        ) from exc
    if resolved != candidate or stat.S_ISLNK(candidate_stat.st_mode):
        raise _ActiveTestScanFailure("CANDIDATE_SYMLINK_OR_ALIAS", candidate)
    if not (stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISREG(candidate_stat.st_mode)):
        raise _ActiveTestScanFailure("CANDIDATE_REGULAR_OR_DIRECTORY_REQUIRED", candidate)
    return candidate


def _active_test_root_presence(path: Path, *, required: bool) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError as exc:
        if required:
            raise _ActiveTestScanFailure("REQUIRED_TEST_ROOT_MISSING", path) from exc
        return False
    except OSError as exc:
        raise _ActiveTestScanFailure(
            f"TEST_ROOT_UNREADABLE:{type(exc).__name__}", path
        ) from exc


def _active_test_roots(candidate: Path) -> list[Path]:
    roots: list[Path] = []
    fixed = BUSINESS / "tests"
    if _lexically_under(candidate, BUSINESS):
        ancestor = candidate.parent
        observed_depth = 0
        while True:
            if observed_depth >= ACTIVE_TEST_SCAN_MAX_ANCESTOR_DEPTH:
                raise _ActiveTestScanFailure(
                    "TEST_ANCESTOR_DEPTH_LIMIT_EXCEEDED", candidate
                )
            test_root = ancestor / "tests"
            if (
                not _lexically_under(test_root, candidate)
                and _active_test_root_presence(test_root, required=False)
            ):
                roots.append(test_root)
            if ancestor == BUSINESS:
                break
            if ancestor == ancestor.parent or not _lexically_under(ancestor, BUSINESS):
                raise _ActiveTestScanFailure("BUSINESS_ANCESTOR_CHAIN_AMBIGUOUS", candidate)
            ancestor = ancestor.parent
            observed_depth += 1

    if not _lexically_under(fixed, candidate) and _active_test_root_presence(
        fixed, required=True
    ):
        roots.append(fixed)

    approved_hermes_repo = HERMES_HOME / "hermes-agent"
    approved_hermes_tests = approved_hermes_repo / "tests"
    if (
        _lexically_under(candidate, approved_hermes_repo)
        and not _lexically_under(approved_hermes_tests, candidate)
        and _active_test_root_presence(approved_hermes_tests, required=False)
    ):
        roots.append(approved_hermes_tests)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    if len(deduped) > ACTIVE_TEST_SCAN_MAX_ROOTS:
        raise _ActiveTestScanFailure(
            f"TEST_ROOT_COUNT_LIMIT_EXCEEDED:{len(deduped)}:{ACTIVE_TEST_SCAN_MAX_ROOTS}",
            candidate,
        )
    return deduped


def _active_test_tokens(candidate: Path) -> list[tuple[str, bytes]]:
    tokens: list[tuple[str, bytes]] = [("canonical_absolute", os.fsencode(str(candidate)))]
    parts = candidate.parts[1:]
    suffixes: list[str] = []
    for index in range(max(0, len(parts) - 32), max(0, len(parts) - 1)):
        value = "/".join(parts[index:])
        if len(parts) - index >= 2 and len(value) <= 4096:
            suffixes.append(value)
    tokens.extend(
        ("ancestor_relative_suffix", os.fsencode(value))
        for value in sorted(set(suffixes), key=lambda item: (-len(item), item))
    )
    # A basename alone is not path authority.  Test policy and fixture builders
    # routinely mention generic cache names (for example ``archive-v0``), and
    # treating those bytes as a production reference creates global false
    # protection.  Canonical absolute paths and path-shaped (two or more
    # component) suffixes remain fail-closed fixture references.
    deduped: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for kind, token in tokens:
        if token and token not in seen:
            seen.add(token)
            deduped.append((kind, token))
    return deduped


@dataclass(frozen=True)
class _ActiveTestRootSnapshot:
    root: Path
    directories: tuple[tuple[Path, tuple[int, ...]], ...]
    files: tuple[tuple[Path, tuple[int, ...], bytes], ...]
    total_bytes: int


class ActiveTestReferenceScan:
    """Bounded, descriptor-bound test-byte index reusable within one discovery."""

    def __init__(self) -> None:
        self.snapshots: dict[str, _ActiveTestRootSnapshot] = {}
        self.files_scanned = 0
        self.directories_scanned = 0
        self.bytes_scanned = 0

    @staticmethod
    def _check_time(started: float, path: Path) -> None:
        if time.monotonic() - started > ACTIVE_TEST_SCAN_MAX_SECONDS:
            raise _ActiveTestScanFailure("TEST_SCAN_MONOTONIC_TIME_LIMIT_EXCEEDED", path)

    @staticmethod
    def _validate_real_root(root: Path) -> None:
        if not root.is_absolute() or Path(os.path.normpath(str(root))) != root:
            raise _ActiveTestScanFailure("TEST_ROOT_NONCANONICAL", root)
        current = Path(root.anchor)
        for component in root.parts[1:]:
            current /= component
            try:
                value = current.lstat()
            except OSError as exc:
                raise _ActiveTestScanFailure(
                    f"TEST_ROOT_COMPONENT_UNREADABLE:{type(exc).__name__}", current
                ) from exc
            if stat.S_ISLNK(value.st_mode):
                raise _ActiveTestScanFailure("TEST_ROOT_SYMLINK_COMPONENT", current)
            if not stat.S_ISDIR(value.st_mode):
                raise _ActiveTestScanFailure("TEST_ROOT_COMPONENT_NOT_DIRECTORY", current)

    def _scan_root(self, root: Path, *, started: float) -> _ActiveTestRootSnapshot:
        self._validate_real_root(root)
        directories: list[tuple[Path, tuple[int, ...]]] = []
        files: list[tuple[Path, tuple[int, ...], bytes]] = []
        root_bytes = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            self._check_time(started, directory)
            if depth > ACTIVE_TEST_SCAN_MAX_RECURSION_DEPTH:
                raise _ActiveTestScanFailure(
                    f"TEST_RECURSION_DEPTH_LIMIT_EXCEEDED:{ACTIVE_TEST_SCAN_MAX_RECURSION_DEPTH}",
                    directory,
                )
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise _ActiveTestScanFailure(
                    f"TEST_DIRECTORY_UNREADABLE:{type(exc).__name__}", directory
                ) from exc
            if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
                raise _ActiveTestScanFailure("TEST_DIRECTORY_REAL_REQUIRED", directory)
            self.directories_scanned += 1
            if self.directories_scanned > ACTIVE_TEST_SCAN_MAX_DIRECTORIES:
                raise _ActiveTestScanFailure(
                    f"TEST_DIRECTORY_COUNT_LIMIT_EXCEEDED:{ACTIVE_TEST_SCAN_MAX_DIRECTORIES}",
                    directory,
                )
            directories.append((directory, _active_test_identity(directory_stat)))
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(list(iterator), key=lambda item: item.name)
            except OSError as exc:
                raise _ActiveTestScanFailure(
                    f"TEST_DIRECTORY_ENUMERATION_FAILED:{type(exc).__name__}", directory
                ) from exc
            for entry in entries:
                self._check_time(started, directory)
                path = directory / entry.name
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise _ActiveTestScanFailure(
                        f"TEST_ENTRY_UNREADABLE:{type(exc).__name__}", path
                    ) from exc
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise _ActiveTestScanFailure("TEST_ENTRY_SYMLINK_FORBIDDEN", path)
                if stat.S_ISDIR(entry_stat.st_mode):
                    if entry.name in {"__pycache__", ".pytest_cache"}:
                        continue
                    stack.append((path, depth + 1))
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise _ActiveTestScanFailure("TEST_ENTRY_SPECIAL_FILE_FORBIDDEN", path)
                if path.suffix in {".pyc", ".pyo"}:
                    continue
                if entry_stat.st_nlink != 1:
                    raise _ActiveTestScanFailure("TEST_FILE_SINGLE_LINK_REQUIRED", path)
                self.files_scanned += 1
                if self.files_scanned > ACTIVE_TEST_SCAN_MAX_FILES:
                    raise _ActiveTestScanFailure(
                        f"TEST_FILE_COUNT_LIMIT_EXCEEDED:{ACTIVE_TEST_SCAN_MAX_FILES}", path
                    )
                if entry_stat.st_size > ACTIVE_TEST_SCAN_MAX_FILE_BYTES:
                    raise _ActiveTestScanFailure(
                        f"TEST_FILE_BYTE_LIMIT_EXCEEDED:{entry_stat.st_size}:"
                        f"{ACTIVE_TEST_SCAN_MAX_FILE_BYTES}",
                        path,
                    )
                if self.bytes_scanned + entry_stat.st_size > ACTIVE_TEST_SCAN_MAX_TOTAL_BYTES:
                    raise _ActiveTestScanFailure(
                        f"TEST_TOTAL_BYTE_LIMIT_EXCEEDED:{ACTIVE_TEST_SCAN_MAX_TOTAL_BYTES}",
                        path,
                    )
                identity = _active_test_identity(entry_stat)
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(path, flags)
                except OSError as exc:
                    raise _ActiveTestScanFailure(
                        f"TEST_FILE_OPEN_FAILED:{type(exc).__name__}", path
                    ) from exc
                try:
                    if _active_test_identity(os.fstat(descriptor)) != identity:
                        raise _ActiveTestScanFailure("TEST_FILE_OPEN_IDENTITY_DRIFT", path)
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        self._check_time(started, path)
                        try:
                            chunk = os.read(descriptor, ACTIVE_TEST_SCAN_CHUNK_BYTES)
                        except OSError as exc:
                            raise _ActiveTestScanFailure(
                                f"TEST_FILE_READ_FAILED:{type(exc).__name__}", path
                            ) from exc
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > ACTIVE_TEST_SCAN_MAX_FILE_BYTES:
                            raise _ActiveTestScanFailure(
                                f"TEST_FILE_BYTE_LIMIT_EXCEEDED:{total}:"
                                f"{ACTIVE_TEST_SCAN_MAX_FILE_BYTES}",
                                path,
                            )
                    _active_test_scan_hook("after_file_read", path)
                    if total != entry_stat.st_size:
                        raise _ActiveTestScanFailure("TEST_FILE_SIZE_DRIFT_DURING_READ", path)
                    if _active_test_identity(os.fstat(descriptor)) != identity:
                        raise _ActiveTestScanFailure("TEST_FILE_DESCRIPTOR_IDENTITY_DRIFT", path)
                    try:
                        path_after = path.lstat()
                    except OSError as exc:
                        raise _ActiveTestScanFailure(
                            f"TEST_FILE_PATH_REVALIDATION_FAILED:{type(exc).__name__}", path
                        ) from exc
                    if _active_test_identity(path_after) != identity:
                        raise _ActiveTestScanFailure("TEST_FILE_PATH_IDENTITY_DRIFT", path)
                finally:
                    os.close(descriptor)
                payload = b"".join(chunks)
                files.append((path, identity, payload))
                self.bytes_scanned += len(payload)
                root_bytes += len(payload)
        _active_test_scan_hook("after_root_scan", root)
        snapshot = _ActiveTestRootSnapshot(
            root=root,
            directories=tuple(sorted(directories, key=lambda item: str(item[0]))),
            files=tuple(sorted(files, key=lambda item: str(item[0]))),
            total_bytes=root_bytes,
        )
        self._validate_snapshot(snapshot, started=started)
        return snapshot

    def _validate_snapshot(
        self, snapshot: _ActiveTestRootSnapshot, *, started: float
    ) -> None:
        for path, identity in snapshot.directories:
            self._check_time(started, path)
            try:
                current = path.lstat()
            except OSError as exc:
                raise _ActiveTestScanFailure(
                    f"TEST_DIRECTORY_REVALIDATION_FAILED:{type(exc).__name__}", path
                ) from exc
            if _active_test_identity(current) != identity:
                raise _ActiveTestScanFailure("TEST_DIRECTORY_IDENTITY_DRIFT", path)
        for path, identity, _payload in snapshot.files:
            self._check_time(started, path)
            try:
                current = path.lstat()
            except OSError as exc:
                raise _ActiveTestScanFailure(
                    f"TEST_FILE_REVALIDATION_FAILED:{type(exc).__name__}", path
                ) from exc
            if _active_test_identity(current) != identity:
                raise _ActiveTestScanFailure("TEST_FILE_IDENTITY_DRIFT", path)

    def _evidence(
        self,
        candidate: Path,
        roots: list[Path],
        *,
        protected: bool,
        test_path: Path | None = None,
        token_kind: str | None = None,
        token: bytes | None = None,
        failure: _ActiveTestScanFailure | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": "active-test-fixture-reference-gate-v1",
            "status": (
                "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED" if protected else "CLEAR"
            ),
            "protected": protected,
            "candidate": str(candidate),
            "test_roots": [str(root) for root in roots],
            "root_count": len(roots),
            "file_count": self.files_scanned,
            "total_bytes": self.bytes_scanned,
            "bounds": {
                "max_roots": ACTIVE_TEST_SCAN_MAX_ROOTS,
                "max_ancestor_depth": ACTIVE_TEST_SCAN_MAX_ANCESTOR_DEPTH,
                "max_recursion_depth": ACTIVE_TEST_SCAN_MAX_RECURSION_DEPTH,
                "max_directories": ACTIVE_TEST_SCAN_MAX_DIRECTORIES,
                "max_files": ACTIVE_TEST_SCAN_MAX_FILES,
                "max_total_bytes": ACTIVE_TEST_SCAN_MAX_TOTAL_BYTES,
                "max_file_bytes": ACTIVE_TEST_SCAN_MAX_FILE_BYTES,
                "max_seconds": ACTIVE_TEST_SCAN_MAX_SECONDS,
            },
        }
        if test_path is not None:
            value["test_path"] = str(test_path)
        if token_kind is not None:
            value["token_kind"] = token_kind
        if token is not None:
            value["token"] = os.fsdecode(token)
        if failure is not None:
            value["failure_reason"] = failure.reason
            if failure.path is not None:
                value["failure_path"] = str(failure.path)
        return value

    def scan(self, candidate: Path) -> dict[str, Any]:
        started = time.monotonic()
        roots: list[Path] = []
        try:
            canonical = _active_test_candidate_path(Path(candidate))
            roots = _active_test_roots(canonical)
            for root in roots:
                key = str(root)
                snapshot = self.snapshots.get(key)
                if snapshot is None:
                    if len(self.snapshots) >= ACTIVE_TEST_SCAN_MAX_ROOTS:
                        raise _ActiveTestScanFailure(
                            f"TEST_ROOT_COUNT_LIMIT_EXCEEDED:{ACTIVE_TEST_SCAN_MAX_ROOTS}",
                            root,
                        )
                    snapshot = self._scan_root(root, started=started)
                    self.snapshots[key] = snapshot
                else:
                    self._validate_snapshot(snapshot, started=started)
            for token_kind, token in _active_test_tokens(canonical):
                for root in roots:
                    snapshot = self.snapshots[str(root)]
                    for test_path, _identity, payload in snapshot.files:
                        self._check_time(started, test_path)
                        if token in payload:
                            return self._evidence(
                                canonical,
                                roots,
                                protected=True,
                                test_path=test_path,
                                token_kind=token_kind,
                                token=token,
                            )
            return self._evidence(canonical, roots, protected=False)
        except _ActiveTestScanFailure as exc:
            candidate_value = Path(candidate)
            return self._evidence(
                candidate_value,
                roots,
                protected=True,
                failure=exc,
            )

    def refreshable_identity_drift(self, evidence: dict[str, Any]) -> bool:
        """Return true only for metadata drift on the same cached test object.

        A changed inode, type, mode, or owner remains fail closed.  The caller
        may replace this cache only after a complete fresh scan of current
        bytes, which preserves real fixture references and hostile anomalies.
        """

        reason = evidence.get("failure_reason")
        failure_path = evidence.get("failure_path")
        if reason not in {
            "TEST_DIRECTORY_IDENTITY_DRIFT",
            "TEST_FILE_IDENTITY_DRIFT",
        } or not isinstance(failure_path, str):
            return False
        path = Path(failure_path)
        expected: tuple[int, ...] | None = None
        for snapshot in self.snapshots.values():
            for cached_path, identity in snapshot.directories:
                if cached_path == path:
                    expected = identity
                    break
            if expected is None:
                for cached_path, identity, _payload in snapshot.files:
                    if cached_path == path:
                        expected = identity
                        break
            if expected is not None:
                break
        if expected is None:
            return False
        try:
            current = _active_test_identity(path.lstat())
        except OSError:
            return False
        # device, inode, mode, uid and gid must remain identical.  Only link,
        # size and timestamp metadata may trigger a fresh current-byte scan.
        return current[:5] == expected[:5]


def active_test_reference_gate(
    candidate: Path, *, scan: ActiveTestReferenceScan | None = None
) -> dict[str, Any]:
    return (scan or ActiveTestReferenceScan()).scan(Path(candidate))


def protected(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return any(
        resolved == root.resolve(strict=False)
        or under(resolved, root)
        or under(root, resolved)
        for root in PROTECTED
    )


def excluded_from_scan(path: Path) -> bool:
    """Defense in depth for roots F12 is never authorized to inspect."""
    resolved = path.resolve(strict=False)
    if "node_modules" in path.parts:
        return True
    return any(
        resolved == root.resolve(strict=False) or under(resolved, root)
        for root in EXCLUDED_SCAN_ROOTS
    )


def _type_name(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular_file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _identity_from_stat(value: os.stat_result) -> dict[str, Any]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "type": _type_name(value.st_mode),
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "mode": value.st_mode,
    }


def source_identity(path: Path) -> dict[str, Any]:
    return _identity_from_stat(path.lstat())


def sha256_file(path: Path, *, deadline: RunDeadline | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk_index, chunk in enumerate(iter(lambda: fh.read(8 * 1024 * 1024), b"")):
            if chunk_index % 16 == 0:
                _check_deadline(deadline, "sha256_file")
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_descriptor(
    descriptor: int, *, deadline: RunDeadline | None = None
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        _check_deadline(deadline, "sha256_descriptor")
        chunk = os.read(descriptor, 8 * 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_regular_unchanged(
    path: Path,
    expected: os.stat_result,
    *,
    deadline: RunDeadline | None = None,
) -> str:
    """Hash a regular file without following a replacement symlink."""

    expected_identity = _identity_from_stat(expected)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if _identity_from_stat(os.fstat(descriptor)) != expected_identity:
            raise SourceDriftError(f"content_open_identity_drift:{path}")
        digest = _sha256_descriptor(descriptor, deadline=deadline)
        if _identity_from_stat(os.fstat(descriptor)) != expected_identity:
            raise SourceDriftError(f"content_read_identity_drift:{path}")
    finally:
        os.close(descriptor)
    if source_identity(path) != expected_identity:
        raise SourceDriftError(f"content_path_identity_drift:{path}")
    return digest


def allocated_bytes(path: Path, *, deadline: RunDeadline | None = None) -> int:
    operation_deadline = deadline or RunDeadline.start(900.0)
    proc = run_process_group(
        ["du", "-sx", "--block-size=1", str(path)],
        deadline=operation_deadline,
        cap_seconds=900.0,
        stage="allocated_bytes_du",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"du_failed:{proc.stderr[-500:]}")
    try:
        return int(proc.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


def _normalize_process_reference(value: str) -> str | None:
    cleaned = value.removesuffix(" (deleted)").rstrip(")],;:\\")
    if not cleaned.startswith("/"):
        return None
    try:
        return str(Path(cleaned).resolve(strict=False))
    except (OSError, ValueError):
        return os.path.normpath(cleaned)


def process_inventory(
    *,
    deadline: RunDeadline | None = None,
    exclude_pids: set[int] | None = None,
) -> ProcessReferenceSnapshot:
    """Capture argv/env/cwd plus every fd/maps path exactly once.

    ``path_is_active`` only consults this immutable snapshot.  It never opens
    ``/proc`` itself, so candidate and cron-member counts cannot multiply proc
    scans.
    """

    rows: list[dict[str, Any]] = []
    references: set[str] = set()
    shallow_references: set[str] = set()
    pids_seen = fd_links_read = maps_files_read = maps_lines_read = 0
    _check_deadline(deadline, "process_snapshot:start")
    try:
        proc_roots = sorted(
            (path for path in Path("/proc").iterdir() if path.name.isdigit()),
            key=lambda path: int(path.name),
        )
    except OSError:
        proc_roots = []
    for proc_index, proc_dir in enumerate(proc_roots):
        if proc_index % 32 == 0:
            _check_deadline(deadline, "process_snapshot:pids")
        process_pid = int(proc_dir.name)
        if exclude_pids and process_pid in exclude_pids:
            continue
        pids_seen += 1
        try:
            cmdline = (
                (proc_dir / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
            environ = (
                (proc_dir / "environ")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        row: dict[str, Any] = {
            "pid": int(proc_dir.name),
            "cmdline": cmdline,
            "cwd": "/",
            "environ": environ,
        }
        for match in ABSOLUTE_REF_RE.findall(f"{cmdline} {environ}"):
            normalized = _normalize_process_reference(match)
            if normalized is not None:
                references.add(normalized)
                shallow_references.add(normalized)
        for name in ("cwd", "exe", "root"):
            try:
                value = os.readlink(proc_dir / name).removesuffix(" (deleted)")
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            row[name] = value
            normalized = _normalize_process_reference(value)
            if normalized is not None:
                references.add(normalized)
                shallow_references.add(normalized)
        try:
            descriptors = sorted((proc_dir / "fd").iterdir(), key=lambda path: path.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            descriptors = []
        for fd_index, descriptor in enumerate(descriptors):
            if fd_index % 128 == 0:
                _check_deadline(deadline, "process_snapshot:fd")
            try:
                value = os.readlink(descriptor).removesuffix(" (deleted)")
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            fd_links_read += 1
            normalized = _normalize_process_reference(value)
            if normalized is not None:
                references.add(normalized)
        try:
            with (proc_dir / "maps").open("r", encoding="utf-8", errors="ignore") as handle:
                maps_files_read += 1
                for line_index, line in enumerate(handle):
                    if line_index % 512 == 0:
                        _check_deadline(deadline, "process_snapshot:maps")
                    maps_lines_read += 1
                    fields = line.split(maxsplit=5)
                    if len(fields) == 6 and fields[5].startswith("/"):
                        normalized = _normalize_process_reference(fields[5].strip())
                        if normalized is not None:
                            references.add(normalized)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            pass
        rows.append(row)
    _check_deadline(deadline, "process_snapshot:complete")
    return ProcessReferenceSnapshot(
        rows=rows,
        references=tuple(sorted(references)),
        captured_monotonic=time.monotonic(),
        proc_pids_seen=pids_seen,
        fd_links_read=fd_links_read,
        maps_files_read=maps_files_read,
        maps_lines_read=maps_lines_read,
        shallow_references=tuple(sorted(shallow_references)),
    )


def reference_under(value: str, root: Path) -> bool:
    value = value.removesuffix(" (deleted)")
    try:
        return under(Path(value), root)
    except (OSError, ValueError):
        return False


def _process_rows(
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return processes.rows if isinstance(processes, ProcessReferenceSnapshot) else processes


def _sorted_references_under(
    absolute: str, references: tuple[str, ...]
) -> bool:
    index = bisect_left(references, absolute)
    if index >= len(references):
        return False
    value = references[index]
    if value == absolute:
        return True
    prefix = absolute if absolute == os.sep else absolute.rstrip(os.sep) + os.sep
    return value.startswith(prefix)


def path_is_active(
    path: Path,
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    *,
    deep: bool = True,
) -> bool:
    """Check one path against a reusable, logarithmically queried snapshot."""

    absolute = str(path.resolve(strict=False))
    if isinstance(processes, ProcessReferenceSnapshot):
        references = (
            processes.references if deep else processes.shallow_references
        )
        return _sorted_references_under(absolute, references)

    rows = _process_rows(processes)
    for row in rows:
        for field in ("cwd", "exe", "root"):
            if reference_under(str(row.get(field, "/")), path):
                return True
        if absolute in str(row.get("cmdline", "")) or absolute in str(row.get("environ", "")):
            return True
    if not deep:
        return False
    references = (
        str(value)
        for row in rows
        for value in row.get("references", ())
        if isinstance(value, str)
    )
    return any(reference_under(value, path) for value in references)


def _iter_source_entries(
    path: Path,
    *,
    deadline: RunDeadline | None = None,
    stage: str = "source_inventory",
) -> Iterable[tuple[str, Path, os.stat_result]]:
    try:
        initial = path.lstat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise RuntimeError(f"source_changed_during_inventory:{path}") from exc
    if stat.S_ISLNK(initial.st_mode) or not (
        stat.S_ISREG(initial.st_mode) or stat.S_ISDIR(initial.st_mode)
    ):
        raise RuntimeError(f"regular_file_or_directory_required:{path}")
    stack: list[tuple[str, Path]] = [(".", path)]
    visited = 0
    while stack:
        if visited % 256 == 0:
            _check_deadline(deadline, f"{stage}:walk")
        visited += 1
        rel, current = stack.pop()
        try:
            current_stat = current.lstat()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise RuntimeError(f"source_changed_during_inventory:{current}") from exc
        yield rel, current, current_stat
        if stat.S_ISDIR(current_stat.st_mode) and not stat.S_ISLNK(current_stat.st_mode):
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise RuntimeError(f"source_changed_during_inventory:{current}") from exc
            for child in children:
                child_rel = child.name if rel == "." else f"{rel}/{child.name}"
                stack.append((child_rel, child))


def source_content_snapshot(
    path: Path, *, deadline: RunDeadline | None = None
) -> dict[str, Any]:
    """Return a content-bound, exact member manifest for a source object."""

    rows: list[tuple[str, str, str | None]] = []
    inode_hashes: dict[tuple[int, int, int, int], str] = {}
    for rel, entry, entry_stat in _iter_source_entries(
        path, deadline=deadline, stage="source_content_snapshot"
    ):
        _check_deadline(deadline, "source_content_snapshot:hash")
        kind = _type_name(entry_stat.st_mode)
        if stat.S_ISREG(entry_stat.st_mode):
            key = (
                entry_stat.st_dev,
                entry_stat.st_ino,
                entry_stat.st_size,
                entry_stat.st_mtime_ns,
            )
            digest = inode_hashes.get(key)
            if digest is None:
                digest = _sha256_regular_unchanged(
                    entry, entry_stat, deadline=deadline
                )
                inode_hashes[key] = digest
            else:
                # Even when the bytes are cached for an in-tree hardlink, bind
                # this directory entry to the expected inode and metadata.
                if source_identity(entry) != _identity_from_stat(entry_stat):
                    raise SourceDriftError(f"content_hardlink_identity_drift:{entry}")
            rows.append((rel, kind, digest))
        elif stat.S_ISLNK(entry_stat.st_mode):
            target = os.readlink(entry)
            if source_identity(entry) != _identity_from_stat(entry_stat):
                raise SourceDriftError(f"content_symlink_identity_drift:{entry}")
            rows.append((rel, kind, target))
        elif stat.S_ISDIR(entry_stat.st_mode):
            rows.append((rel, kind, None))
        else:
            raise SourceDriftError(f"unsupported_source_member_type:{entry}")
    manifest_sha = hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return {"sha256": manifest_sha, "rows": rows}


def tree_stats(
    path: Path,
    *,
    ignore_ephemeral: bool = False,
    include_manifest: bool = True,
    deadline: RunDeadline | None = None,
) -> dict[str, Any]:
    files = 0
    logical = 0
    newest = 0.0
    newest_path = str(path)
    contains_node_modules = False
    manifest: list[tuple[Any, ...]] = []
    inode_entries: dict[tuple[int, int], list[os.stat_result]] = {}
    for rel, entry, entry_stat in _iter_source_entries(
        path, deadline=deadline, stage="tree_stats"
    ):
        if rel != "." and "node_modules" in Path(rel).parts:
            contains_node_modules = True
        if ignore_ephemeral and rel != ".":
            name = entry.name
            if name in IGNORED_ACTIVITY_NAMES or name.endswith(IGNORED_ACTIVITY_SUFFIXES):
                continue
        kind = _type_name(entry_stat.st_mode)
        if stat.S_ISREG(entry_stat.st_mode):
            files += 1
            logical += entry_stat.st_size
        if entry_stat.st_mtime > newest:
            newest = entry_stat.st_mtime
            newest_path = str(entry)
        key = (entry_stat.st_dev, entry_stat.st_ino)
        inode_entries.setdefault(key, []).append(entry_stat)
        if include_manifest:
            manifest.append(
                (
                    rel,
                    kind,
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                    entry_stat.st_mode,
                    entry_stat.st_uid,
                    entry_stat.st_gid,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    entry_stat.st_nlink,
                    entry_stat.st_blocks,
                    os.readlink(entry) if stat.S_ISLNK(entry_stat.st_mode) else None,
                )
            )
    allocated = sum(rows[0].st_blocks * 512 for rows in inode_entries.values())
    externally_linked = 0
    external_inode_count = 0
    for rows in inode_entries.values():
        first = rows[0]
        if stat.S_ISREG(first.st_mode) and first.st_nlink > len(rows):
            externally_linked += first.st_blocks * 512
            external_inode_count += 1
    manifest_sha = None
    if include_manifest:
        manifest_sha = hashlib.sha256(
            json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    return {
        "files": files,
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "externally_linked_allocated_bytes": externally_linked,
        "reclaimable_allocated_bytes": max(0, allocated - externally_linked),
        "hardlink_external_inode_count": external_inode_count,
        "contains_node_modules": contains_node_modules,
        "newest_mtime": newest,
        "newest_path": newest_path,
        "manifest_sha256": manifest_sha,
        # Kept in memory for descriptor-safe removal reconciliation.  Callers do
        # not place these potentially large rows in terminal receipts.
        "manifest_rows": manifest if include_manifest else None,
    }


def safe_symlinks(path: Path, *, deadline: RunDeadline | None = None) -> bool:
    try:
        root_stat = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(root_stat.st_mode):
        return False
    if stat.S_ISREG(root_stat.st_mode):
        return True
    if not stat.S_ISDIR(root_stat.st_mode):
        return False
    root = path.resolve(strict=False)
    try:
        items = path.rglob("*")
        for index, item in enumerate(items):
            if index % 256 == 0:
                _check_deadline(deadline, "safe_symlinks:walk")
            if item.is_symlink():
                try:
                    item.resolve(strict=False).relative_to(root)
                except (ValueError, OSError):
                    return False
    except OSError:
        return False
    return True


def fast_tree_stats(
    path: Path,
    retention_seconds: int,
    minimum_bytes: int = 0,
    *,
    deadline: RunDeadline | None = None,
) -> dict[str, Any] | None:
    """Eligibility-only stats for one direct regular file or directory."""
    try:
        stats = tree_stats(path, include_manifest=False, deadline=deadline)
    except (RuntimeError, OSError):
        return None
    if stats["contains_node_modules"]:
        return None
    cutoff = time.time() - retention_seconds
    # "Older than" is strict: an exact-boundary object remains retained.
    if stats["newest_mtime"] >= cutoff:
        return None
    if minimum_bytes:
        stats["allocated_bytes"] = (
            allocated_bytes(path, deadline=deadline)
            if deadline is not None
            else allocated_bytes(path)
        )
        stats["reclaimable_allocated_bytes"] = max(
            0,
            stats["allocated_bytes"]
            - stats["externally_linked_allocated_bytes"],
        )
    if stats["allocated_bytes"] < minimum_bytes:
        return None
    return stats


def _read_json_bytes(path: Path, *, label: str, expected_sha256: str | None = None) -> tuple[dict[str, Any], bytes]:
    try:
        entry_stat = path.lstat()
    except FileNotFoundError as exc:
        raise SiteAuthorityError(f"{label}_missing:{path}") from exc
    except OSError as exc:
        raise SiteAuthorityError(f"{label}_unreadable:{path}") from exc
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise SiteAuthorityError(f"{label}_regular_file_required:{path}")
    if entry_stat.st_size > AUTHORITY_MAX_BYTES:
        raise SiteAuthorityError(f"{label}_oversized:{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SiteAuthorityError(f"{label}_unreadable:{path}") from exc
    if expected_sha256 is not None:
        if not SHA256_RE.fullmatch(expected_sha256):
            raise SiteAuthorityError(f"{label}_expected_hash_malformed")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise SiteAuthorityError(f"{label}_hash_invalid:{path}")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SiteAuthorityError(f"{label}_malformed:{path}") from exc
    if not isinstance(value, dict):
        raise SiteAuthorityError(f"{label}_object_required:{path}")
    return value, raw


def _canonical_base_path() -> Path:
    return REPORTS / "glp-clinic-scout" / "seo-machine" / "current-content-base.json"


def _validate_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SiteAuthorityError(f"{label}_hash_malformed")
    return value


def _site_direct_reference(
    value: Any,
    *,
    label: str,
    lanes: set[str] | None = None,
    require_directory: bool = False,
) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise SiteAuthorityError(f"{label}_absolute_path_required")
    path = Path(value)
    allowed = lanes or set(SITE_LANE_RETENTION_SECONDS)
    matched = False
    for lane in allowed:
        root = FLAGSHIP / lane
        try:
            if path.resolve(strict=False).parent == root.resolve(strict=False):
                matched = True
                break
        except OSError:
            pass
    if not matched:
        raise SiteAuthorityError(f"{label}_outside_direct_site_lane:{path}")
    try:
        entry_stat = path.lstat()
    except OSError as exc:
        raise SiteAuthorityError(f"{label}_missing_or_unreadable:{path}") from exc
    if stat.S_ISLNK(entry_stat.st_mode) or not (
        stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode)
    ):
        raise SiteAuthorityError(f"{label}_unsafe_type:{path}")
    if require_directory and not stat.S_ISDIR(entry_stat.st_mode):
        raise SiteAuthorityError(f"{label}_directory_required:{path}")
    return path.resolve(strict=False)


def _evidence_entry(base: dict[str, Any], key: str) -> tuple[Path, str]:
    entry = base.get(key)
    if not isinstance(entry, dict):
        raise SiteAuthorityError(f"current_content_{key}_binding_missing")
    raw_path = entry.get("path")
    raw_sha = entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise SiteAuthorityError(f"current_content_{key}_path_invalid")
    path = Path(raw_path)
    evidence_root = REPORTS / "glp-clinic-scout" / "seo-machine"
    if not under(path, evidence_root):
        raise SiteAuthorityError(f"current_content_{key}_outside_evidence_root")
    return path, _validate_hash(raw_sha, f"current_content_{key}")


def _receipt_deployment_id(receipt: dict[str, Any]) -> Any:
    deployment = receipt.get("deployment")
    if isinstance(deployment, dict):
        return deployment.get("deployment_id")
    return receipt.get("deployment_id")


def _collect_explicit_rollback_values(payload: Any) -> list[str]:
    values: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"rollback_release_root", "rollback_release"} and isinstance(value, str):
                values.append(value)
            elif key == "rollback" and isinstance(value, dict):
                candidate = value.get("release_root") or value.get("path")
                if isinstance(candidate, str):
                    values.append(candidate)
            if isinstance(value, (dict, list)):
                values.extend(_collect_explicit_rollback_values(value))
    elif isinstance(payload, list):
        for value in payload:
            values.extend(_collect_explicit_rollback_values(value))
    return values


def _validate_predecessor_identity_evidence(
    deployment_id: str, release_root: str
) -> dict[str, str]:
    """Prove a predecessor UUID/root pair from its canonical remint evidence.

    The pointer's two strings are not self-authenticating at a destructive
    boundary.  A prior canonical remint supplies an independently bound
    manifest/root and terminal receipt/deployment pair.
    """
    prefix = deployment_id.split("-", 1)[0]
    roots = sorted((REPORTS / "glp-clinic-scout" / "seo-machine").glob(
        f"base-remint-{prefix}-*"
    ))
    proved: list[dict[str, str]] = []
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        manifest_path = root / "01-deployment-manifest.json"
        receipt_path = root / "10-final-publication-receipt.json"
        if not manifest_path.is_file() or not receipt_path.is_file():
            continue
        manifest, manifest_raw = _read_json_bytes(
            manifest_path, label="predecessor_content_manifest"
        )
        receipt, receipt_raw = _read_json_bytes(
            receipt_path, label="predecessor_content_final_receipt"
        )
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        if (
            manifest.get("schema") == "glp-opus-release-deployment-manifest-v1"
            and manifest.get("release_root") == release_root
            and receipt.get("schema") == "glp-opus-production-terminal-receipt-v1"
            and receipt.get("status") == "PASS_PRODUCTION_TERMINAL"
            and _receipt_deployment_id(receipt) == deployment_id
            and receipt.get("evidence_sha256") == {manifest_path.name: manifest_sha}
        ):
            proved.append({
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha,
                "final_receipt_path": str(receipt_path),
                "final_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
            })
    if not proved:
        raise SiteAuthorityError("current_content_predecessor_identity_unproved")
    # 2026-08-27: ambiguity means CONFLICTING authorities, not the same authority
    # at two paths. Rapid same-day supersession (three launch-night remints) left
    # the predecessor's proof both at its live dir and at its content-addressed
    # "-preserved-" dir — byte-identical manifest+receipt pairs. Distinctness is
    # therefore judged on proof CONTENT: byte-different proofs claiming the same
    # deployment still abort the destructive boundary exactly as before.
    if len({(row["manifest_sha256"], row["final_receipt_sha256"]) for row in proved}) != 1:
        raise SiteAuthorityError("current_content_predecessor_identity_ambiguous")
    return proved[0]


# Process-local only: kernel ctime/identity revalidation, never a TTL or persisted
# trust bit. Retention rechecks prepared authority before every member unlink;
# rehashing an unchanged entire site at each boundary makes that loop unbounded.
_PREPARED_EVIDENCE_CACHE: dict[str, Any] | None = None
_PREPARED_CACHE_MAX_ENTRIES = 100_000


def _prepared_tree_identity(root: Path) -> tuple[Any, ...] | None:
    """Complete path-set + lstat identity, or decline caching (not validation).

    Include ctime: rewriting bytes and restoring mtime cannot reuse a proof.
    Ancestor identities bind the namespace without treating unrelated sibling
    writes as changes to this prepared tree. Symlinks/special files never cache.
    """
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root:
            return None
        rows: list[Any] = []
        for parent in root.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                return None
            rows.append((str(parent), info.st_dev, info.st_ino, info.st_mode,
                         info.st_uid, info.st_gid))
        pending = [root]
        while pending:
            path = pending.pop()
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return None
            rows.append((str(path), info.st_dev, info.st_ino, info.st_mode,
                         info.st_uid, info.st_gid, info.st_nlink, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns))
            if len(rows) > _PREPARED_CACHE_MAX_ENTRIES:
                return None
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append(Path(entry.path))
                        if len(rows) + len(pending) > _PREPARED_CACHE_MAX_ENTRIES:
                            return None
        return tuple(sorted(rows))
    except OSError:
        return None


def _prepared_dependencies_unchanged(evidence: dict[str, Any]) -> bool:
    """Rehash every producer-returned external/small dependency on each use."""
    try:
        from glp_price_index_weekly import _read_bound_regular_file
        files = evidence['prepared_binding']['files']
        if not isinstance(files, dict) or not files:
            return False
        for path, (expected_sha, expected_identity) in files.items():
            _path, body, identity = _read_bound_regular_file(path, 'retention_prepared_dependency')
            if hashlib.sha256(body).hexdigest() != expected_sha or identity != expected_identity:
                return False
        return True
    except (ImportError, KeyError, TypeError, ValueError, RuntimeError, OSError):
        return False


def _registered_prepared_release_evidence(base: dict[str, Any], release: Path) -> dict[str, Any]:
    """Reuse a full native proof only while its whole content/namespace is unchanged."""
    import copy
    from glp_price_index_weekly import _validate_base_evidence
    global _PREPARED_EVIDENCE_CACHE
    pointer, pointer_raw = _read_json_bytes(_canonical_base_path(), label='prepared_cache_pointer')
    if pointer != base:
        raise SiteAuthorityError('prepared_pointer_input_changed')
    # The native producer binds exact canonical-pointer bytes, not merely its
    # parsed JSON. Formatting-only rewrites must also force native validation.
    key = (str(release), json.dumps(base, sort_keys=True, separators=(',', ':')),
           hashlib.sha256(pointer_raw).hexdigest())
    before = _prepared_tree_identity(release.parent)
    cached = _PREPARED_EVIDENCE_CACHE
    if (before is not None and cached is not None and cached['key'] == key
            and cached['identity'] == before
            and _prepared_dependencies_unchanged(cached['evidence'])
            and _prepared_tree_identity(release.parent) == before
            and _read_json_bytes(_canonical_base_path(), label='prepared_cache_pointer')[1] == pointer_raw):
        return copy.deepcopy(cached['evidence'])
    # No stale fallback: every mismatch, unreadable path, membership change or
    # unsupported shape invokes the unchanged native transitive validator.
    _PREPARED_EVIDENCE_CACHE = None
    evidence = _validate_base_evidence(base, release)
    after = _prepared_tree_identity(release.parent)
    if (before is not None and before == after
            and _prepared_dependencies_unchanged(evidence)
            and _prepared_tree_identity(release.parent) == after
            and _read_json_bytes(_canonical_base_path(), label='prepared_cache_pointer')[1] == pointer_raw):
        _PREPARED_EVIDENCE_CACHE = {'key': key, 'identity': after,
                                    'evidence': copy.deepcopy(evidence)}
    return evidence


def load_site_tree_authority() -> dict[str, Any]:
    """Load and cross-validate the canonical current/predecessor authority chain."""
    base_path = _canonical_base_path()
    base, base_raw = _read_json_bytes(base_path, label="current_content_base")
    prepared_format = is_prepared_pointer(base)
    try:
        validate_pointer_shape(
            base,
            # The registered prepared publisher owns a reports/ops release;
            # its full native evidence, not a broad reports-root lane, proves it.
            releases_root=None if prepared_format else FLAGSHIP / "releases",
            require_release_dirs=True,
        )
    except RuntimeError as exc:
        raise SiteAuthorityError(f"current_content_base_pointer_invalid:{exc}") from exc
    prepared_binding = None
    if prepared_format:
        try:
            prepared_evidence = _registered_prepared_release_evidence(base, Path(base["release_root"]))
            prepared_binding = prepared_evidence["prepared_binding"]
            if not prepared_binding:
                raise RuntimeError("prepared_binding_missing")
        except (ImportError, RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
            raise SiteAuthorityError(f"current_content_prepared_evidence_invalid:{exc}") from exc
    deployment_id = base.get("deployment_id")
    if not isinstance(deployment_id, str) or not deployment_id:
        raise SiteAuthorityError("current_content_base_deployment_id_invalid")
    current = (Path(base["release_root"]) if prepared_format else _site_direct_reference(
        base.get("release_root"),
        label="current_release",
        lanes={"releases"},
        require_directory=True,
    ))
    release_tree_sha = _validate_hash(base.get("release_tree_sha256"), "current_release_tree")
    public_tree_sha = _validate_hash(base.get("public_tree_sha256"), "current_public_tree")

    manifest_path, manifest_sha = _evidence_entry(base, "manifest")
    receipt_path, receipt_sha = _evidence_entry(base, "final_receipt")
    manifest, _ = _read_json_bytes(
        manifest_path, label="current_content_manifest", expected_sha256=manifest_sha
    )
    receipt, _ = _read_json_bytes(
        receipt_path, label="current_content_final_receipt", expected_sha256=receipt_sha
    )
    if not isinstance(manifest.get("schema"), str):
        raise SiteAuthorityError("current_content_manifest_schema_invalid")
    manifest_release = (manifest.get("release_root") if prepared_format else _site_direct_reference(
        manifest.get("release_root"),
        label="manifest_release",
        lanes={"releases"},
        require_directory=True,
    ))
    if manifest_release != (str(current) if prepared_format else current):
        raise SiteAuthorityError("current_content_manifest_release_mismatch")
    if manifest.get("release_tree_sha256") != release_tree_sha:
        raise SiteAuthorityError("current_content_manifest_tree_hash_mismatch")
    if (
        "public_tree_sha256" in manifest
        and manifest.get("public_tree_sha256") != public_tree_sha
    ):
        raise SiteAuthorityError("current_content_manifest_public_hash_mismatch")
    if "deployment_id" in manifest and manifest.get("deployment_id") != deployment_id:
        raise SiteAuthorityError("current_content_manifest_deployment_mismatch")

    terminal_statuses = {"PASS_PRODUCTION_TERMINAL", "PASS_LIVE_GSC_READBACK_PASS"}
    if receipt.get("status") not in terminal_statuses or not isinstance(receipt.get("schema"), str):
        raise SiteAuthorityError("current_content_final_receipt_schema_or_status_invalid")
    if _receipt_deployment_id(receipt) != deployment_id:
        raise SiteAuthorityError("current_content_final_receipt_deployment_mismatch")
    evidence = receipt.get("evidence_sha256")
    if isinstance(evidence, dict):
        if evidence.get(manifest_path.name) != manifest_sha:
            raise SiteAuthorityError("current_content_final_receipt_manifest_hash_mismatch")
    else:
        receipt_manifest = receipt.get("manifest")
        if not isinstance(receipt_manifest, dict) or receipt_manifest.get("sha256") != manifest_sha:
            raise SiteAuthorityError("current_content_final_receipt_manifest_binding_missing")
    closure = receipt.get("immutable_closure")
    if isinstance(closure, dict):
        if closure.get("release_tree_sha256") != release_tree_sha:
            raise SiteAuthorityError("current_content_final_receipt_release_hash_mismatch")
        if closure.get("public_tree_sha256") != public_tree_sha:
            raise SiteAuthorityError("current_content_final_receipt_public_hash_mismatch")
    else:
        if receipt.get("release_tree_sha256") != release_tree_sha:
            raise SiteAuthorityError("current_content_final_receipt_release_hash_mismatch")
        if receipt.get("public_tree_sha256") != public_tree_sha:
            raise SiteAuthorityError("current_content_final_receipt_public_hash_mismatch")

    refs: set[Path] = {current}
    predecessor: Path | None = None
    supersedes = base["supersedes"]
    # The shared schema has already required both exact predecessor identities.
    predecessor = _site_direct_reference(
        supersedes["release_root"],
        label="predecessor_release",
        lanes={"releases"},
        require_directory=True,
    )
    predecessor_identity_evidence = _validate_predecessor_identity_evidence(
        supersedes["deployment_id"], str(predecessor)
    )
    refs.add(predecessor)
    for payload in (base, manifest, receipt):
        for value in _collect_explicit_rollback_values(payload):
            refs.add(
                _site_direct_reference(
                    value,
                    label="canonical_rollback_release",
                    lanes={"releases"},
                    require_directory=True,
                )
            )
    if prepared_format:
        # Re-run the registered read-only transitive check after consuming the
        # pointer/manifest/receipt; a same-run Worker or preparation rewrite
        # must not survive on the strength of their outer hashes alone.
        try:
            if _registered_prepared_release_evidence(base, current)["prepared_binding"] != prepared_binding:
                raise RuntimeError("prepared_binding_changed")
            if _read_json_bytes(base_path, label="current_content_base")[1] != base_raw:
                raise RuntimeError("prepared_pointer_changed")
        except (ImportError, RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
            raise SiteAuthorityError(f"current_content_prepared_evidence_drift:{exc}") from exc
    return {
        "status": "VALID",
        "base_path": str(base_path),
        "base_sha256": hashlib.sha256(base_raw).hexdigest(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "final_receipt_path": str(receipt_path),
        "final_receipt_sha256": receipt_sha,
        "current_release": str(current),
        "predecessor_release": str(predecessor) if predecessor else None,
        "predecessor_deployment_id": supersedes["deployment_id"],
        "predecessor_identity_evidence": predecessor_identity_evidence,
        "prepared_binding": prepared_binding,
        "deployment_id": deployment_id,
        "refs": refs,
    }


def _iter_receipt_json(
    root: Path,
    *,
    recent_cutoff: float | None = None,
    deadline: RunDeadline | None = None,
) -> Iterable[Path]:
    try:
        root_stat = root.lstat()
    except OSError:
        return
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return
    for walk_index, (walk_root, dirs, files) in enumerate(os.walk(root, followlinks=False)):
        if walk_index % 64 == 0:
            _check_deadline(deadline, "receipt_json:walk")
        walk_path = Path(walk_root)
        dirs[:] = sorted(
            d for d in dirs if not (walk_path / d).is_symlink() and d != "node_modules"
        )
        for name in sorted(files):
            # Active rollback and recent-mission authority may be carried by a
            # generically named JSON artifact.  Inspect every bounded JSON file
            # rather than making filename heuristics an expiry mechanism.
            if not name.lower().endswith(".json"):
                continue
            source = walk_path / name
            try:
                source_stat = source.lstat()
            except OSError:
                continue
            if (
                stat.S_ISLNK(source_stat.st_mode)
                or not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_size > ROLLBACK_SCAN_MAX_BYTES
                or (recent_cutoff is not None and source_stat.st_mtime < recent_cutoff)
            ):
                continue
            yield source


def _effective_rollback_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    status_value = payload.get("status")
    status = str(status_value).upper() if isinstance(status_value, str) else ""
    if status in ACTIVE_ROLLBACK_STATUSES:
        return status
    if payload.get("active") is True:
        return "ACTIVE"
    if payload.get("rollback_active") is True:
        return "ROLLBACK_ACTIVE"
    rollback = payload.get("rollback")
    if isinstance(rollback, dict):
        rollback_status = str(rollback.get("status") or "").upper()
        if rollback_status in ACTIVE_ROLLBACK_STATUSES:
            return rollback_status
        if rollback.get("active") is True:
            return "ACTIVE"
    return ""


def _payload_has_active_rollback(payload: Any) -> bool:
    return bool(_effective_rollback_status(payload))


def _safe_reference_from_text(value: str) -> Path | None:
    path = Path(value)
    for lane in SITE_LANE_RETENTION_SECONDS:
        root = FLAGSHIP / lane
        try:
            if path.resolve(strict=False).parent == root.resolve(strict=False):
                return path.resolve(strict=False)
        except OSError:
            continue
    return None


def _all_string_values(payload: Any) -> Iterable[str]:
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _all_string_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _all_string_values(value)


def _legacy_rollback_scan_time_check(
    started: float,
    deadline: RunDeadline | None,
    stage: str,
) -> None:
    _check_deadline(deadline, stage)
    if time.monotonic() - started > ROLLBACK_SCAN_MAX_SECONDS:
        raise SiteAuthorityError(f"active_rollback_index_runtime_budget_exceeded:{stage}")


def _legacy_rollback_authority_candidate_name(name: str) -> bool:
    """Admit only explicitly named rollback/deployment authority artifacts."""

    lowered = name.lower()
    if not lowered.endswith(".json"):
        return False
    return any(
        fragment in lowered for fragment in ROLLBACK_AUTHORITY_NAME_FRAGMENTS
    ) or lowered.endswith(ROLLBACK_AUTHORITY_NAME_SUFFIXES)


def _legacy_rollback_candidate_bytes(
    source: Path,
    source_stat: os.stat_result,
    *,
    started: float,
    deadline: RunDeadline | None,
) -> tuple[bytes, str]:
    """Descriptor-read one admitted authority candidate and bind its digest."""

    if source_stat.st_size > ROLLBACK_SCAN_MAX_BYTES:
        raise SiteAuthorityError(f"active_rollback_authority_file_oversized:{source}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SiteAuthorityError(
            f"active_rollback_index_file_unreadable:{source}:{type(exc).__name__}:{exc}"
        ) from exc
    expected = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    buffered = bytearray()
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        observed = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if not stat.S_ISREG(opened.st_mode) or observed != expected:
            raise SiteAuthorityError(
                f"active_rollback_authority_candidate_identity_drift:{source}"
            )
        chunk_index = 0
        while True:
            if chunk_index % 16 == 0:
                _legacy_rollback_scan_time_check(
                    started, deadline, "active_rollback_index:candidate_content"
                )
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            buffered.extend(chunk)
            digest.update(chunk)
            if len(buffered) > ROLLBACK_SCAN_MAX_BYTES:
                raise SiteAuthorityError(
                    f"active_rollback_authority_file_oversized:{source}"
                )
            chunk_index += 1
        final = os.fstat(descriptor)
        final_observed = (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        )
        if final_observed != expected or len(buffered) != source_stat.st_size:
            raise SiteAuthorityError(
                f"active_rollback_authority_candidate_identity_drift:{source}"
            )
    finally:
        os.close(descriptor)
    return bytes(buffered), digest.hexdigest()


def _legacy_build_active_rollback_index(
    *,
    root: Path | None = None,
    deadline: RunDeadline | None = None,
) -> ActiveRollbackIndex:
    """Build one strict, bounded active-rollback authority index.

    The whole JSON corpus is metadata-inventoried exactly once, but content is
    opened only for explicit rollback/deployment authority filenames. Generic
    unreadable JSON is unrelated by construction; an unreadable, malformed, or
    over-budget admitted candidate fails closed for all site lanes.
    """

    authority_root = Path(root or REPORTS)
    started = time.monotonic()
    try:
        root_stat = authority_root.lstat()
    except OSError as exc:
        raise SiteAuthorityError(
            f"active_rollback_authority_root_missing_or_unreadable:{authority_root}"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise SiteAuthorityError(
            f"active_rollback_authority_root_real_directory_required:{authority_root}"
        )

    refs: set[Path] = set()
    candidate_fingerprints: dict[str, tuple[int, int, int, int, str]] = {}
    file_count = total_bytes = relevant_count = relevant_bytes = parsed_count = 0
    walk_errors: list[OSError] = []

    def walk_error(exc: OSError) -> None:
        walk_errors.append(exc)

    for walk_index, (walk_root, dirs, files) in enumerate(
        os.walk(authority_root, followlinks=False, onerror=walk_error)
    ):
        if walk_index % 64 == 0:
            _legacy_rollback_scan_time_check(started, deadline, "active_rollback_index:walk")
        if walk_errors:
            exc = walk_errors[0]
            raise SiteAuthorityError(
                f"active_rollback_index_walk_incomplete:{exc.filename}:{exc.strerror}"
            )
        walk_path = Path(walk_root)
        traversable: list[str] = []
        for name in sorted(dirs):
            child = walk_path / name
            if name == "node_modules":
                continue
            try:
                child_stat = child.lstat()
            except OSError as exc:
                raise SiteAuthorityError(
                    f"active_rollback_index_directory_unreadable:{child}:{type(exc).__name__}:{exc}"
                ) from exc
            if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(child_stat.st_mode):
                traversable.append(name)
        dirs[:] = traversable
        for name in sorted(files):
            if not name.lower().endswith(".json"):
                continue
            _legacy_rollback_scan_time_check(started, deadline, "active_rollback_index:files")
            source = walk_path / name
            try:
                source_stat = source.lstat()
            except OSError as exc:
                raise SiteAuthorityError(
                    f"active_rollback_index_file_unreadable:{source}:{type(exc).__name__}:{exc}"
                ) from exc
            file_count += 1
            total_bytes += source_stat.st_size
            if file_count > ROLLBACK_SCAN_MAX_FILES:
                raise SiteAuthorityError(
                    f"active_rollback_index_file_count_budget_exceeded:{file_count}"
                )
            if total_bytes > ROLLBACK_SCAN_MAX_TOTAL_BYTES:
                raise SiteAuthorityError(
                    f"active_rollback_index_total_bytes_budget_exceeded:{total_bytes}"
                )
            if not _legacy_rollback_authority_candidate_name(name):
                # Filename admission happens before open: unrelated unreadable
                # JSON can never accidentally become rollback authority.
                continue
            if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
                raise SiteAuthorityError(
                    f"active_rollback_index_candidate_regular_file_required:{source}"
                )
            raw, digest = _legacy_rollback_candidate_bytes(
                source,
                source_stat,
                started=started,
                deadline=deadline,
            )
            relevant_count += 1
            relevant_bytes += source_stat.st_size
            try:
                payload = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SiteAuthorityError(
                    f"active_rollback_authority_malformed:{source}:{type(exc).__name__}:{exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise SiteAuthorityError(
                    f"active_rollback_authority_object_required:{source}"
                )
            if "schema" in payload and (
                not isinstance(payload["schema"], str) or not payload["schema"].strip()
            ):
                raise SiteAuthorityError(
                    f"active_rollback_authority_schema_invalid:{source}"
                )
            parsed_count += 1
            candidate_fingerprints[str(source)] = (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                digest,
            )
            if not _payload_has_active_rollback(payload):
                continue
            values = _collect_explicit_rollback_values(payload)
            if not values:
                raise SiteAuthorityError(
                    f"active_rollback_authority_explicit_reference_required:{source}"
                )
            for raw_value in values:
                candidate = _safe_reference_from_text(raw_value)
                if candidate is None:
                    raise SiteAuthorityError(
                        f"active_rollback_reference_invalid:{source}:{raw_value}"
                    )
                refs.add(candidate)
    if walk_errors:
        exc = walk_errors[0]
        raise SiteAuthorityError(
            f"active_rollback_index_walk_incomplete:{exc.filename}:{exc.strerror}"
        )
    _legacy_rollback_scan_time_check(started, deadline, "active_rollback_index:complete")
    return ActiveRollbackIndex(
        refs=refs,
        json_file_count=file_count,
        json_total_bytes=total_bytes,
        relevant_file_count=relevant_count,
        relevant_total_bytes=relevant_bytes,
        parsed_file_count=parsed_count,
        scan_elapsed_seconds=time.monotonic() - started,
        candidate_fingerprints=candidate_fingerprints,
        authority_root=authority_root,
    )


def _legacy_validate_active_rollback_index(
    index: ActiveRollbackIndex,
    *,
    root: Path | None = None,
    deadline: RunDeadline | None = None,
) -> None:
    """Revalidate the built-once candidate set and bytes without reparsing it.

    Every later destructive checkpoint calls this bounded validator. A new,
    removed, renamed, unreadable, or byte-changed admitted authority candidate
    blocks site work, so a newly activated rollback pin cannot be missed.
    Unrelated JSON remains metadata-only and is never opened.
    """

    authority_root = Path(root or index.authority_root or REPORTS)
    started = time.monotonic()
    try:
        root_stat = authority_root.lstat()
    except OSError as exc:
        raise SiteAuthorityError(
            f"active_rollback_authority_root_missing_or_unreadable:{authority_root}"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise SiteAuthorityError(
            f"active_rollback_authority_root_real_directory_required:{authority_root}"
        )

    current: dict[str, os.stat_result] = {}
    file_count = total_bytes = 0
    walk_errors: list[OSError] = []

    def walk_error(exc: OSError) -> None:
        walk_errors.append(exc)

    for walk_index, (walk_root, dirs, files) in enumerate(
        os.walk(authority_root, followlinks=False, onerror=walk_error)
    ):
        if walk_index % 64 == 0:
            _legacy_rollback_scan_time_check(
                started, deadline, "active_rollback_index_validation:walk"
            )
        if walk_errors:
            exc = walk_errors[0]
            raise SiteAuthorityError(
                f"active_rollback_index_validation_walk_incomplete:{exc.filename}:{exc.strerror}"
            )
        walk_path = Path(walk_root)
        traversable: list[str] = []
        for name in sorted(dirs):
            child = walk_path / name
            if name == "node_modules":
                continue
            try:
                child_stat = child.lstat()
            except OSError as exc:
                raise SiteAuthorityError(
                    f"active_rollback_index_validation_directory_unreadable:{child}:{type(exc).__name__}:{exc}"
                ) from exc
            if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(child_stat.st_mode):
                traversable.append(name)
        dirs[:] = traversable
        for name in sorted(files):
            if not name.lower().endswith(".json"):
                continue
            _legacy_rollback_scan_time_check(
                started, deadline, "active_rollback_index_validation:files"
            )
            source = walk_path / name
            try:
                source_stat = source.lstat()
            except OSError as exc:
                raise SiteAuthorityError(
                    f"active_rollback_index_validation_file_unreadable:{source}:{type(exc).__name__}:{exc}"
                ) from exc
            file_count += 1
            total_bytes += source_stat.st_size
            if file_count > ROLLBACK_SCAN_MAX_FILES:
                raise SiteAuthorityError(
                    f"active_rollback_index_file_count_budget_exceeded:{file_count}"
                )
            if total_bytes > ROLLBACK_SCAN_MAX_TOTAL_BYTES:
                raise SiteAuthorityError(
                    f"active_rollback_index_total_bytes_budget_exceeded:{total_bytes}"
                )
            if not _legacy_rollback_authority_candidate_name(name):
                continue
            if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
                raise SiteAuthorityError(
                    f"active_rollback_index_candidate_regular_file_required:{source}"
                )
            current[str(source)] = source_stat

    if walk_errors:
        exc = walk_errors[0]
        raise SiteAuthorityError(
            f"active_rollback_index_validation_walk_incomplete:{exc.filename}:{exc.strerror}"
        )
    expected_paths = set(index.candidate_fingerprints)
    current_paths = set(current)
    if current_paths != expected_paths:
        added = sorted(current_paths - expected_paths)
        removed = sorted(expected_paths - current_paths)
        raise SiteAuthorityError(
            "active_rollback_authority_candidate_set_drift:"
            f"added={added[:3]}:removed={removed[:3]}"
        )
    for source_text in sorted(current):
        source = Path(source_text)
        source_stat = current[source_text]
        _raw, digest = _legacy_rollback_candidate_bytes(
            source,
            source_stat,
            started=started,
            deadline=deadline,
        )
        observed = (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            digest,
        )
        if observed != index.candidate_fingerprints[source_text]:
            raise SiteAuthorityError(
                f"active_rollback_authority_candidate_byte_drift:{source}"
            )
    _legacy_rollback_scan_time_check(
        started, deadline, "active_rollback_index_validation:complete"
    )


def _legacy_active_rollback_references(
    *, deadline: RunDeadline | None = None
) -> set[Path]:
    """Compatibility accessor backed by the bounded canonical run index."""

    return set(build_active_rollback_index(deadline=deadline).refs)


def _authority_file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def _authority_root_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
    )


def _registry_mode(value: Any, *, label: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-7]{4}", value):
        raise SiteAuthorityError(f"active_rollback_registry_{label}_mode_invalid")
    return int(value, 8)


def _registry_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SiteAuthorityError(f"active_rollback_registry_{label}_invalid")
    return value


def _registry_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SiteAuthorityError(f"active_rollback_registry_{label}_sha256_invalid")
    return value

def _canonical_authority_path(
    path: Path, *, label: str, require_directory: bool = False
) -> tuple[Path, os.stat_result]:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise SiteAuthorityError(f"active_rollback_registry_{label}_canonical_path_required:{path}")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_unreadable:{current}:{type(exc).__name__}:{exc}"
            ) from exc
        if stat.S_ISLNK(value.st_mode):
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_symlink_forbidden:{current}"
            )
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_parent_directory_required:{current}"
            )
    if require_directory:
        if not stat.S_ISDIR(value.st_mode):
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_directory_required:{path}"
            )
    elif not stat.S_ISREG(value.st_mode):
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_regular_file_required:{path}"
        )
    return path, value


def _read_registry_file_once(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    expected_identity: tuple[int, ...] | None = None,
    retain_descriptor: bool = False,
    deadline: RunDeadline | None = None,
) -> tuple[bytes, str, tuple[int, ...], int]:
    _check_deadline(deadline, f"active_rollback_registry:{label}:before_open")
    try:
        canonical, before = _canonical_authority_path(path, label=label)
    except SiteAuthorityError as exc:
        if label == "registry" and "unreadable" in str(exc):
            raise SiteAuthorityError(
                f"active_rollback_registry_missing_or_unreadable:{path}"
            ) from exc
        raise
    identity = _authority_file_identity(before)
    if expected_identity is not None and identity != expected_identity:
        raise SiteAuthorityError(f"active_rollback_registry_{label}_identity_drift:{path}")
    if before.st_size > max_bytes:
        raise SiteAuthorityError(f"active_rollback_registry_{label}_oversized:{path}")
    if before.st_nlink != 1:
        raise SiteAuthorityError(f"active_rollback_registry_{label}_link_count_invalid:{path}")
    if stat.S_IMODE(before.st_mode) & 0o022:
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_group_or_world_writable:{path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(canonical, flags)
    except OSError as exc:
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_unreadable:{path}:{type(exc).__name__}:{exc}"
        ) from exc
    raw = bytearray()
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _authority_file_identity(opened) != identity:
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_identity_drift:{path}"
            )
        while True:
            _check_deadline(deadline, f"active_rollback_registry:{label}:read")
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            digest.update(chunk)
            if len(raw) > max_bytes:
                raise SiteAuthorityError(
                    f"active_rollback_registry_{label}_oversized:{path}"
                )
        final = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_identity_drift:{path}"
            ) from exc
        if (
            _authority_file_identity(final) != identity
            or _authority_file_identity(path_after) != identity
            or len(raw) != identity[6]
        ):
            raise SiteAuthorityError(
                f"active_rollback_registry_{label}_identity_drift:{path}"
            )
        return bytes(raw), digest.hexdigest(), identity, descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        if not retain_descriptor:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _authority_entry_set_digest(
    active_entries: list[dict[str, Any]],
    recent_entries: list[dict[str, Any]],
) -> str:
    return _canonical_json_sha256(
        {
            "active_rollback_entries": active_entries,
            "recent_reference_entries": recent_entries,
        }
    )


def _normalize_registry_root(
    value: Any,
    *,
    entry_index: int,
    label: str,
    require_directory: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_root_binding_invalid:{entry_index}"
        )
    expected_keys = {
        "path",
        "device",
        "inode",
        "uid",
        "gid",
        "mode",
        "nlink",
        "type",
    }
    if set(value) != expected_keys:
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_root_schema_invalid:{entry_index}"
        )
    path_text = value.get("path")
    if (
        not isinstance(path_text, str)
        or not path_text
        or not Path(path_text).is_absolute()
        or Path(os.path.normpath(path_text)) != Path(path_text)
    ):
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_root_path_invalid:{entry_index}"
        )
    root_type = value.get("type")
    allowed_types = {"directory"} if require_directory else {"directory", "regular_file"}
    if root_type not in allowed_types:
        raise SiteAuthorityError(
            f"active_rollback_registry_{label}_root_type_invalid:{entry_index}"
        )
    identity = (
        _registry_integer(value.get("device"), label=f"{label}_root_device"),
        _registry_integer(value.get("inode"), label=f"{label}_root_inode"),
        _registry_integer(value.get("uid"), label=f"{label}_root_uid"),
        _registry_integer(value.get("gid"), label=f"{label}_root_gid"),
        _registry_mode(value.get("mode"), label=f"{label}_root"),
        _registry_integer(value.get("nlink"), label=f"{label}_root_nlink"),
    )
    return {"path": Path(path_text), "identity": identity, "type": root_type}


def _normalize_registry_entry(value: Any, *, entry_index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_binding_invalid:{entry_index}"
        )
    expected_keys = {
        "authority_path",
        "sha256",
        "device",
        "inode",
        "uid",
        "gid",
        "mode",
        "nlink",
        "size_bytes",
        "mtime_ns",
        "schema",
        "status",
        "expiry_policy",
        "rollback_release_roots",
    }
    if set(value) != expected_keys:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_schema_invalid:{entry_index}"
        )
    path_text = value.get("authority_path")
    schema = value.get("schema")
    status_value = value.get("status")
    if not isinstance(path_text, str) or not path_text:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_path_invalid:{entry_index}"
        )
    if not isinstance(schema, str) or not schema:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_schema_invalid:{entry_index}"
        )
    if not isinstance(status_value, str) or status_value.upper() not in ACTIVE_ROLLBACK_STATUSES:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_status_invalid:{entry_index}"
        )
    if value.get("expiry_policy") != ACTIVE_ROLLBACK_EXPIRY_POLICY:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_expiry_policy_invalid:{entry_index}"
        )
    roots_value = value.get("rollback_release_roots")
    if (
        not isinstance(roots_value, list)
        or not roots_value
        or len(roots_value) > ROLLBACK_REGISTRY_MAX_ROOTS_PER_ENTRY
    ):
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_roots_invalid:{entry_index}"
        )
    roots = tuple(
        _normalize_registry_root(
            root,
            entry_index=entry_index,
            label="active",
            require_directory=True,
        )
        for root in roots_value
    )
    paths = [str(root["path"]) for root in roots]
    if len(paths) != len(set(paths)):
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_duplicate_root:{entry_index}"
        )
    identity = (
        _registry_integer(value.get("device"), label="entry_device"),
        _registry_integer(value.get("inode"), label="entry_inode"),
        _registry_integer(value.get("uid"), label="entry_uid"),
        _registry_integer(value.get("gid"), label="entry_gid"),
        _registry_mode(value.get("mode"), label="entry"),
        _registry_integer(value.get("nlink"), label="entry_nlink"),
        _registry_integer(value.get("size_bytes"), label="entry_size_bytes"),
        _registry_integer(value.get("mtime_ns"), label="entry_mtime_ns"),
    )
    return {
        "authority_path": Path(path_text),
        "sha256": _registry_sha256(value.get("sha256"), label="entry"),
        "identity": identity,
        "schema": schema,
        "status": status_value.upper(),
        "expiry_policy": ACTIVE_ROLLBACK_EXPIRY_POLICY,
        "roots": roots,
    }


def _normalize_recent_registry_entry(
    value: Any, *, entry_index: int
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_binding_invalid:{entry_index}"
        )
    expected_keys = {
        "authority_path",
        "sha256",
        "device",
        "inode",
        "uid",
        "gid",
        "mode",
        "nlink",
        "size_bytes",
        "mtime_ns",
        "schema",
        "status",
        "retention_status",
        "expiry_policy",
        "expires_at_unix_ns",
        "reference_roots",
    }
    if set(value) != expected_keys:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_schema_invalid:{entry_index}"
        )
    path_text = value.get("authority_path")
    schema = value.get("schema")
    source_status = value.get("status")
    if not isinstance(path_text, str) or not path_text:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_path_invalid:{entry_index}"
        )
    if not isinstance(schema, str) or not schema:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_source_schema_invalid:{entry_index}"
        )
    if not isinstance(source_status, str) or not source_status:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_source_status_invalid:{entry_index}"
        )
    if value.get("retention_status") != RECENT_REFERENCE_STATUS:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_status_invalid:{entry_index}"
        )
    if value.get("expiry_policy") != RECENT_REFERENCE_EXPIRY_POLICY:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_expiry_policy_invalid:{entry_index}"
        )
    expires_at_unix_ns = _registry_integer(
        value.get("expires_at_unix_ns"), label="recent_expires_at_unix_ns"
    )
    mtime_ns = _registry_integer(
        value.get("mtime_ns"), label="recent_entry_mtime_ns"
    )
    if expires_at_unix_ns != (
        mtime_ns + RECENT_REFERENCE_WINDOW_SECONDS * 1_000_000_000
    ):
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_expiry_drift:{entry_index}"
        )
    roots_value = value.get("reference_roots")
    if (
        not isinstance(roots_value, list)
        or not roots_value
        or len(roots_value) > ROLLBACK_REGISTRY_MAX_ROOTS_PER_ENTRY
    ):
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_roots_invalid:{entry_index}"
        )
    roots = tuple(
        _normalize_registry_root(
            root,
            entry_index=entry_index,
            label="recent",
            require_directory=False,
        )
        for root in roots_value
    )
    paths = [str(root["path"]) for root in roots]
    if len(paths) != len(set(paths)):
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_duplicate_root:{entry_index}"
        )
    identity = (
        _registry_integer(value.get("device"), label="recent_entry_device"),
        _registry_integer(value.get("inode"), label="recent_entry_inode"),
        _registry_integer(value.get("uid"), label="recent_entry_uid"),
        _registry_integer(value.get("gid"), label="recent_entry_gid"),
        _registry_mode(value.get("mode"), label="recent_entry"),
        _registry_integer(value.get("nlink"), label="recent_entry_nlink"),
        _registry_integer(value.get("size_bytes"), label="recent_entry_size_bytes"),
        mtime_ns,
    )
    return {
        "authority_path": Path(path_text),
        "sha256": _registry_sha256(value.get("sha256"), label="recent_entry"),
        "identity": identity,
        "schema": schema,
        "status": source_status,
        "retention_status": RECENT_REFERENCE_STATUS,
        "expiry_policy": RECENT_REFERENCE_EXPIRY_POLICY,
        "expires_at_unix_ns": expires_at_unix_ns,
        "roots": roots,
    }


def _exact_registry_ownership(value: Any, *, label: str) -> dict[str, Any]:
    expected = {
        "owner": ROLLBACK_REGISTRY_OWNER,
        "authority": ROLLBACK_REGISTRY_AUTHORITY,
        "update_policy": ROLLBACK_REGISTRY_UPDATE_POLICY,
        "automatic_runtime_discovery_allowed": False,
    }
    if (
        not isinstance(value, dict)
        or set(value) != set(expected)
        or value.get("owner") != expected["owner"]
        or value.get("authority") != expected["authority"]
        or value.get("update_policy") != expected["update_policy"]
        or value.get("automatic_runtime_discovery_allowed") is not False
    ):
        raise SiteAuthorityError(f"active_rollback_registry_{label}_ownership_invalid")
    return dict(expected)


def _validated_historical_bootstrap(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value != HISTORICAL_BOOTSTRAP_PROVENANCE:
        raise SiteAuthorityError(
            "active_rollback_registry_historical_bootstrap_provenance_invalid"
        )
    return dict(HISTORICAL_BOOTSTRAP_PROVENANCE)


def _normalize_generation_evidence_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteAuthorityError("active_rollback_registry_generation_evidence_invalid")
    expected_keys = {
        "path",
        "sha256",
        "size_bytes",
        "device",
        "inode",
        "uid",
        "gid",
        "mode",
        "nlink",
        "mtime_ns",
        "schema",
        "generation",
        "inventory_digest_sha256",
        "entry_set_digest_sha256",
    }
    if set(value) != expected_keys:
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_schema_invalid"
        )
    path_text = value.get("path")
    if (
        not isinstance(path_text, str)
        or not Path(path_text).is_absolute()
        or Path(os.path.normpath(path_text)) != Path(path_text)
    ):
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_path_invalid"
        )
    schema = value.get("schema")
    generation = value.get("generation")
    if schema != ROLLBACK_GENERATION_EVIDENCE_SCHEMA:
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_schema_invalid"
        )
    if not isinstance(generation, str):
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_generation_invalid"
        )
    identity = (
        _registry_integer(value.get("device"), label="evidence_device"),
        _registry_integer(value.get("inode"), label="evidence_inode"),
        _registry_integer(value.get("uid"), label="evidence_uid"),
        _registry_integer(value.get("gid"), label="evidence_gid"),
        _registry_mode(value.get("mode"), label="evidence"),
        _registry_integer(value.get("nlink"), label="evidence_nlink"),
        _registry_integer(value.get("size_bytes"), label="evidence_size_bytes"),
        _registry_integer(value.get("mtime_ns"), label="evidence_mtime_ns"),
    )
    return {
        "path": Path(path_text),
        "sha256": _registry_sha256(value.get("sha256"), label="evidence"),
        "identity": identity,
        "schema": schema,
        "generation": generation,
        "inventory_digest_sha256": _registry_sha256(
            value.get("inventory_digest_sha256"), label="evidence_inventory_digest"
        ),
        "entry_set_digest_sha256": _registry_sha256(
            value.get("entry_set_digest_sha256"), label="evidence_entry_set_digest"
        ),
    }


@dataclass(frozen=True)
class _ValidatedRetentionRegistry:
    generation: str
    active_entries: tuple[dict[str, Any], ...]
    recent_entries: tuple[dict[str, Any], ...]
    historical_bootstrap: dict[str, Any]
    current_entry_counts: dict[str, int]
    entry_set_digest_sha256: str
    inventory_digest_sha256: str
    evidence_binding: dict[str, Any]
    raw_entry_set: dict[str, Any]


def _validate_registry_payload(payload: Any) -> _ValidatedRetentionRegistry:
    if not isinstance(payload, dict):
        raise SiteAuthorityError("active_rollback_registry_object_required")
    expected_keys = {
        "schema",
        "immutable_version",
        "generation",
        "ownership",
        "historical_bootstrap",
        "current_entry_counts",
        "active_rollback_entries",
        "recent_reference_entries",
        "entry_set_digest_sha256",
        "generation_evidence",
    }
    if set(payload) != expected_keys:
        raise SiteAuthorityError("active_rollback_registry_top_level_schema_invalid")
    if payload.get("schema") != ROLLBACK_REGISTRY_SCHEMA:
        raise SiteAuthorityError("active_rollback_registry_schema_invalid")
    if payload.get("immutable_version") != ROLLBACK_REGISTRY_IMMUTABLE_VERSION:
        raise SiteAuthorityError("active_rollback_registry_immutable_version_invalid")
    generation = payload.get("generation")
    if (
        not isinstance(generation, str)
        or len(generation) < 16
        or len(generation) > 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]+", generation)
    ):
        raise SiteAuthorityError("active_rollback_registry_generation_invalid")
    _exact_registry_ownership(payload.get("ownership"), label="registry")
    historical = _validated_historical_bootstrap(payload.get("historical_bootstrap"))
    active_values = payload.get("active_rollback_entries")
    recent_values = payload.get("recent_reference_entries")
    if not isinstance(active_values, list) or not isinstance(recent_values, list):
        raise SiteAuthorityError("active_rollback_registry_entries_invalid")
    if len(active_values) + len(recent_values) > ROLLBACK_REGISTRY_MAX_ENTRIES:
        raise SiteAuthorityError("active_rollback_registry_entry_count_exceeded")
    counts_value = payload.get("current_entry_counts")
    if not isinstance(counts_value, dict) or set(counts_value) != {
        "active_rollback_entry_count",
        "recent_reference_entry_count",
        "total_entry_count",
    }:
        raise SiteAuthorityError("active_rollback_registry_current_counts_invalid")
    counts = {
        key: _registry_integer(value, label=f"current_{key}")
        for key, value in counts_value.items()
    }
    if (
        counts["active_rollback_entry_count"] != len(active_values)
        or counts["recent_reference_entry_count"] != len(recent_values)
        or counts["total_entry_count"] != len(active_values) + len(recent_values)
    ):
        raise SiteAuthorityError("active_rollback_registry_current_count_drift")
    entries = tuple(
        _normalize_registry_entry(entry, entry_index=index)
        for index, entry in enumerate(active_values)
    )
    recent_entries = tuple(
        _normalize_recent_registry_entry(entry, entry_index=index)
        for index, entry in enumerate(recent_values)
    )
    entry_paths = [str(entry["authority_path"]) for entry in (*entries, *recent_entries)]
    if len(entry_paths) != len(set(entry_paths)):
        raise SiteAuthorityError("active_rollback_registry_duplicate_entry_path")
    computed_digest = _authority_entry_set_digest(active_values, recent_values)
    declared_digest = _registry_sha256(
        payload.get("entry_set_digest_sha256"), label="entry_set_digest"
    )
    if declared_digest != computed_digest:
        raise SiteAuthorityError("active_rollback_registry_entry_set_digest_drift")
    evidence_binding = _normalize_generation_evidence_binding(
        payload.get("generation_evidence")
    )
    inventory_digest = str(historical["inventory_sha256"])
    if evidence_binding["generation"] != generation:
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_generation_drift"
        )
    if evidence_binding["inventory_digest_sha256"] != inventory_digest:
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_inventory_digest_drift"
        )
    if evidence_binding["entry_set_digest_sha256"] != computed_digest:
        raise SiteAuthorityError(
            "active_rollback_registry_generation_evidence_entry_set_digest_drift"
        )
    return _ValidatedRetentionRegistry(
        generation=generation,
        active_entries=entries,
        recent_entries=recent_entries,
        historical_bootstrap=historical,
        current_entry_counts=counts,
        entry_set_digest_sha256=computed_digest,
        inventory_digest_sha256=inventory_digest,
        evidence_binding=evidence_binding,
        raw_entry_set={
            "active_rollback_entries": active_values,
            "recent_reference_entries": recent_values,
        },
    )


def _validate_registered_entry(
    entry: dict[str, Any], *, deadline: RunDeadline | None = None
) -> set[Path]:
    path = entry["authority_path"]
    raw, digest, _identity, _descriptor = _read_registry_file_once(
        path,
        max_bytes=ROLLBACK_AUTHORITY_ENTRY_MAX_BYTES,
        label="entry",
        expected_identity=entry["identity"],
        deadline=deadline,
    )
    if digest != entry["sha256"]:
        raise SiteAuthorityError(f"active_rollback_registry_entry_sha256_drift:{path}")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SiteAuthorityError(
            f"active_rollback_registry_entry_malformed:{path}:{type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SiteAuthorityError(f"active_rollback_registry_entry_object_required:{path}")
    if payload.get("schema") != entry["schema"]:
        raise SiteAuthorityError(f"active_rollback_registry_entry_schema_drift:{path}")
    if _effective_rollback_status(payload) != entry["status"]:
        raise SiteAuthorityError(f"active_rollback_registry_entry_status_drift:{path}")
    values = _collect_explicit_rollback_values(payload)
    expected_paths = {str(root["path"]): root for root in entry["roots"]}
    if set(values) != set(expected_paths):
        raise SiteAuthorityError(f"active_rollback_registry_entry_root_drift:{path}")
    refs: set[Path] = set()
    releases = (FLAGSHIP / "releases").resolve(strict=False)
    for root_text in sorted(expected_paths):
        root_binding = expected_paths[root_text]
        root_path, root_stat = _canonical_authority_path(
            root_binding["path"], label="root", require_directory=True
        )
        try:
            resolved = root_path.resolve(strict=True)
        except OSError as exc:
            raise SiteAuthorityError(
                f"active_rollback_registry_root_unreadable:{root_path}:{exc}"
            ) from exc
        if resolved != root_path or resolved.parent != releases:
            raise SiteAuthorityError(
                f"active_rollback_registry_root_invalid_release_root:{root_path}"
            )
        if _authority_root_identity(root_stat) != root_binding["identity"]:
            raise SiteAuthorityError(
                f"active_rollback_registry_root_identity_drift:{root_path}"
            )
        refs.add(resolved)
    return refs


def _validate_registered_recent_entry(
    entry: dict[str, Any],
    *,
    deadline: RunDeadline | None = None,
    now_ns: int | None = None,
) -> set[Path]:
    path = entry["authority_path"]
    raw, digest, _identity, _descriptor = _read_registry_file_once(
        path,
        max_bytes=ROLLBACK_AUTHORITY_ENTRY_MAX_BYTES,
        label="recent_entry",
        expected_identity=entry["identity"],
        deadline=deadline,
    )
    if digest != entry["sha256"]:
        raise SiteAuthorityError(f"recent_reference_registry_entry_sha256_drift:{path}")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SiteAuthorityError(
            f"recent_reference_registry_entry_malformed:{path}:{type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SiteAuthorityError(f"recent_reference_registry_entry_object_required:{path}")
    if payload.get("schema") != entry["schema"]:
        raise SiteAuthorityError(f"recent_reference_registry_entry_schema_drift:{path}")
    if payload.get("status") != entry["status"]:
        raise SiteAuthorityError(f"recent_reference_registry_entry_status_drift:{path}")
    expected_paths = {str(root["path"]): root for root in entry["roots"]}
    observed_paths: set[str] = set()
    for text in _all_string_values(payload):
        reference = _safe_reference_from_text(text)
        if reference is not None:
            observed_paths.add(str(reference))
    if observed_paths != set(expected_paths):
        raise SiteAuthorityError(f"recent_reference_registry_entry_root_drift:{path}")
    resolved_roots: set[Path] = set()
    allowed_parents = {
        (FLAGSHIP / lane).resolve(strict=False)
        for lane in SITE_LANE_RETENTION_SECONDS
    }
    for root_text in sorted(expected_paths):
        root_binding = expected_paths[root_text]
        root_path, root_stat = _canonical_authority_path(
            root_binding["path"],
            label="recent_root",
            require_directory=root_binding["type"] == "directory",
        )
        try:
            resolved = root_path.resolve(strict=True)
        except OSError as exc:
            raise SiteAuthorityError(
                f"recent_reference_registry_root_unreadable:{root_path}:{exc}"
            ) from exc
        actual_type = "directory" if stat.S_ISDIR(root_stat.st_mode) else "regular_file"
        if (
            resolved != root_path
            or resolved.parent not in allowed_parents
            or actual_type != root_binding["type"]
        ):
            raise SiteAuthorityError(
                f"recent_reference_registry_root_invalid_site_root:{root_path}"
            )
        if _authority_root_identity(root_stat) != root_binding["identity"]:
            raise SiteAuthorityError(
                f"recent_reference_registry_root_identity_drift:{root_path}"
            )
        resolved_roots.add(resolved)
    if (now_ns if now_ns is not None else time.time_ns()) > entry["expires_at_unix_ns"]:
        return set()
    return resolved_roots


def _validate_generation_evidence_payload(
    payload: Any,
    definition: _ValidatedRetentionRegistry,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SiteAuthorityError("active_rollback_generation_evidence_object_required")
    expected_keys = {
        "schema",
        "immutable_version",
        "generation",
        "ownership",
        "update_provenance",
        "historical_bootstrap",
        "recent_reference_bootstrap",
        "current_entry_counts",
        "inventory_digest_sha256",
        "entry_set_digest_sha256",
        "entry_set",
    }
    if set(payload) != expected_keys:
        raise SiteAuthorityError("active_rollback_generation_evidence_top_level_schema_invalid")
    if payload.get("schema") != ROLLBACK_GENERATION_EVIDENCE_SCHEMA:
        raise SiteAuthorityError("active_rollback_generation_evidence_schema_invalid")
    if (
        payload.get("immutable_version")
        != ROLLBACK_GENERATION_EVIDENCE_IMMUTABLE_VERSION
    ):
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_immutable_version_invalid"
        )
    if payload.get("generation") != definition.generation:
        raise SiteAuthorityError("active_rollback_generation_evidence_generation_drift")
    _exact_registry_ownership(payload.get("ownership"), label="evidence")
    provenance = payload.get("update_provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "schema",
        "mechanism",
        "update_policy",
        "automatic_runtime_discovery_allowed",
        "predecessor_generation",
        "created_at_utc",
        "update_reason",
    }:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_update_provenance_invalid"
        )
    predecessor = provenance.get("predecessor_generation")
    created = provenance.get("created_at_utc")
    reason = provenance.get("update_reason")
    if (
        provenance.get("schema") != ROLLBACK_UPDATE_PROVENANCE_SCHEMA
        or provenance.get("mechanism") != ROLLBACK_REGISTRY_UPDATE_MECHANISM
        or provenance.get("update_policy") != ROLLBACK_REGISTRY_UPDATE_POLICY
        or provenance.get("automatic_runtime_discovery_allowed") is not False
        or (
            predecessor is not None
            and (
                not isinstance(predecessor, str)
                or predecessor == definition.generation
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]+", predecessor)
            )
        )
        or not isinstance(created, str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created)
        or not isinstance(reason, str)
        or not reason
    ):
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_update_policy_drift"
        )
    historical = _validated_historical_bootstrap(payload.get("historical_bootstrap"))
    if historical != definition.historical_bootstrap:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_historical_bootstrap_drift"
        )
    if payload.get("current_entry_counts") != definition.current_entry_counts:
        raise SiteAuthorityError("active_rollback_generation_evidence_current_count_drift")
    inventory_digest = _registry_sha256(
        payload.get("inventory_digest_sha256"), label="evidence_inventory_digest"
    )
    entry_set_digest = _registry_sha256(
        payload.get("entry_set_digest_sha256"), label="evidence_entry_set_digest"
    )
    if inventory_digest != definition.inventory_digest_sha256:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_inventory_digest_drift"
        )
    if entry_set_digest != definition.entry_set_digest_sha256:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_entry_set_digest_drift"
        )
    entry_set = payload.get("entry_set")
    if entry_set != definition.raw_entry_set:
        raise SiteAuthorityError("active_rollback_generation_evidence_entry_set_drift")
    if _canonical_json_sha256(entry_set) != entry_set_digest:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_entry_set_recomputation_drift"
        )
    recent_bootstrap = payload.get("recent_reference_bootstrap")
    if not isinstance(recent_bootstrap, dict) or set(recent_bootstrap) != {
        "schema",
        "captured_at_utc",
        "cutoff_unix_ns",
        "window_seconds",
        "reports_root",
        "eligible_root_count",
        "eligible_root_set_digest_sha256",
        "scanned_json_file_count",
        "scanned_json_total_bytes",
        "contributing_source_count",
        "current_recent_entry_count",
        "scan_errors",
    }:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_recent_bootstrap_invalid"
        )
    if (
        recent_bootstrap.get("schema")
        != "f12-f13-recent-reference-bootstrap-scan-v1"
        or recent_bootstrap.get("window_seconds") != RECENT_REFERENCE_WINDOW_SECONDS
        or not isinstance(recent_bootstrap.get("reports_root"), str)
        or not Path(recent_bootstrap["reports_root"]).is_absolute()
        or Path(os.path.normpath(recent_bootstrap["reports_root"]))
        != Path(recent_bootstrap["reports_root"])
        or not isinstance(recent_bootstrap.get("captured_at_utc"), str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
            recent_bootstrap["captured_at_utc"],
        )
        or any(
            not isinstance(recent_bootstrap.get(key), int)
            or isinstance(recent_bootstrap.get(key), bool)
            or recent_bootstrap[key] < 0
            for key in (
                "cutoff_unix_ns",
                "eligible_root_count",
                "scanned_json_file_count",
                "scanned_json_total_bytes",
                "contributing_source_count",
                "current_recent_entry_count",
            )
        )
        or not SHA256_RE.fullmatch(
            str(recent_bootstrap.get("eligible_root_set_digest_sha256", ""))
        )
        or recent_bootstrap.get("current_recent_entry_count")
        != definition.current_entry_counts["recent_reference_entry_count"]
        or recent_bootstrap.get("contributing_source_count")
        != definition.current_entry_counts["recent_reference_entry_count"]
        or recent_bootstrap.get("scan_errors") != []
    ):
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_recent_bootstrap_drift"
        )
    return {
        "update_provenance": dict(provenance),
        "recent_reference_bootstrap": dict(recent_bootstrap),
    }


def _read_bound_registry_descriptor(
    index: ActiveRollbackIndex, *, deadline: RunDeadline | None = None
) -> bytes:
    if index.closed or index.registry_path is None or index.registry_descriptor < 0:
        raise SiteAuthorityError("active_rollback_registry_descriptor_unavailable")
    path = index.registry_path
    try:
        _canonical, path_stat = _canonical_authority_path(path, label="registry")
        before = os.fstat(index.registry_descriptor)
    except (OSError, SiteAuthorityError) as exc:
        raise SiteAuthorityError(
            f"active_rollback_registry_identity_drift:{path}"
        ) from exc
    if (
        _authority_file_identity(path_stat) != index.registry_identity
        or _authority_file_identity(before) != index.registry_identity
    ):
        raise SiteAuthorityError(f"active_rollback_registry_identity_drift:{path}")
    raw = bytearray()
    digest = hashlib.sha256()
    offset = 0
    while len(raw) <= ROLLBACK_REGISTRY_MAX_BYTES:
        _check_deadline(deadline, "active_rollback_registry:descriptor_read")
        try:
            chunk = os.pread(index.registry_descriptor, 64 * 1024, offset)
        except OSError as exc:
            raise SiteAuthorityError(
                "active_rollback_registry_descriptor_read_failed"
            ) from exc
        if not chunk:
            break
        raw.extend(chunk)
        digest.update(chunk)
        offset += len(chunk)
        if len(raw) > ROLLBACK_REGISTRY_MAX_BYTES:
            raise SiteAuthorityError("active_rollback_registry_oversized")
    try:
        final = os.fstat(index.registry_descriptor)
        path_after = path.lstat()
    except OSError as exc:
        raise SiteAuthorityError(f"active_rollback_registry_identity_drift:{path}") from exc
    if (
        _authority_file_identity(final) != index.registry_identity
        or _authority_file_identity(path_after) != index.registry_identity
        or len(raw) != index.registry_identity[6]
    ):
        raise SiteAuthorityError(f"active_rollback_registry_identity_drift:{path}")
    if digest.hexdigest() != index.registry_sha256:
        raise SiteAuthorityError(f"active_rollback_registry_sha256_drift:{path}")
    return bytes(raw)


def _read_bound_generation_evidence_descriptor(
    index: ActiveRollbackIndex, *, deadline: RunDeadline | None = None
) -> bytes:
    if index.closed or index.evidence_path is None or index.evidence_descriptor < 0:
        raise SiteAuthorityError("active_rollback_generation_evidence_descriptor_unavailable")
    path = index.evidence_path
    try:
        _canonical, path_stat = _canonical_authority_path(
            path, label="generation_evidence"
        )
        before = os.fstat(index.evidence_descriptor)
    except (OSError, SiteAuthorityError) as exc:
        raise SiteAuthorityError(
            f"active_rollback_generation_evidence_identity_drift:{path}"
        ) from exc
    if (
        _authority_file_identity(path_stat) != index.evidence_identity
        or _authority_file_identity(before) != index.evidence_identity
        or stat.S_IMODE(before.st_mode) & 0o222
    ):
        raise SiteAuthorityError(
            f"active_rollback_generation_evidence_identity_drift:{path}"
        )
    raw = bytearray()
    digest = hashlib.sha256()
    offset = 0
    while len(raw) <= ROLLBACK_GENERATION_EVIDENCE_MAX_BYTES:
        _check_deadline(deadline, "active_rollback_generation_evidence:validation_read")
        try:
            chunk = os.pread(index.evidence_descriptor, 64 * 1024, offset)
        except OSError as exc:
            raise SiteAuthorityError(
                "active_rollback_generation_evidence_descriptor_read_failed"
            ) from exc
        if not chunk:
            break
        raw.extend(chunk)
        digest.update(chunk)
        offset += len(chunk)
        if len(raw) > ROLLBACK_GENERATION_EVIDENCE_MAX_BYTES:
            raise SiteAuthorityError("active_rollback_generation_evidence_oversized")
    try:
        final = os.fstat(index.evidence_descriptor)
        path_after = path.lstat()
    except OSError as exc:
        raise SiteAuthorityError(
            f"active_rollback_generation_evidence_identity_drift:{path}"
        ) from exc
    if (
        _authority_file_identity(final) != index.evidence_identity
        or _authority_file_identity(path_after) != index.evidence_identity
        or len(raw) != index.evidence_identity[6]
        or stat.S_IMODE(final.st_mode) & 0o222
    ):
        raise SiteAuthorityError(
            f"active_rollback_generation_evidence_identity_drift:{path}"
        )
    if digest.hexdigest() != index.evidence_sha256:
        raise SiteAuthorityError(
            f"active_rollback_generation_evidence_sha256_drift:{path}"
        )
    return bytes(raw)


def build_active_rollback_index(
    *,
    registry_path: Path | None = None,
    root: Path | None = None,
    deadline: RunDeadline | None = None,
) -> ActiveRollbackIndex:
    """Load one descriptor-bound registry/evidence generation; never walk reports."""

    if root is not None and registry_path is not None:
        raise SiteAuthorityError("active_rollback_registry_path_ambiguous")
    path = Path(registry_path or root or ROLLBACK_REGISTRY_PATH)
    started = time.monotonic()
    descriptor = -1
    evidence_descriptor = -1
    try:
        raw, digest, identity, descriptor = _read_registry_file_once(
            path,
            max_bytes=ROLLBACK_REGISTRY_MAX_BYTES,
            label="registry",
            retain_descriptor=True,
            deadline=deadline,
        )
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SiteAuthorityError(
                f"active_rollback_registry_malformed:{path}:{type(exc).__name__}:{exc}"
            ) from exc
        definition = _validate_registry_payload(payload)
        binding = definition.evidence_binding
        (
            evidence_raw,
            evidence_digest,
            evidence_identity,
            evidence_descriptor,
        ) = _read_registry_file_once(
            binding["path"],
            max_bytes=ROLLBACK_GENERATION_EVIDENCE_MAX_BYTES,
            label="generation_evidence",
            retain_descriptor=True,
            expected_identity=binding["identity"],
            deadline=deadline,
        )
        if stat.S_IMODE(evidence_identity[4]) & 0o222:
            raise SiteAuthorityError(
                "active_rollback_generation_evidence_not_immutable"
            )
        if evidence_digest != binding["sha256"]:
            raise SiteAuthorityError(
                "active_rollback_generation_evidence_sha256_drift"
            )
        try:
            evidence_payload = json.loads(evidence_raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SiteAuthorityError(
                "active_rollback_generation_evidence_malformed"
            ) from exc
        _validate_generation_evidence_payload(evidence_payload, definition)
        active_refs: set[Path] = set()
        for entry in definition.active_entries:
            active_refs.update(_validate_registered_entry(entry, deadline=deadline))
        recent_refs: set[Path] = set()
        now_ns = time.time_ns()
        for entry in definition.recent_entries:
            recent_refs.update(
                _validate_registered_recent_entry(
                    entry, deadline=deadline, now_ns=now_ns
                )
            )
        all_entries = (*definition.active_entries, *definition.recent_entries)
        return ActiveRollbackIndex(
            refs=active_refs | recent_refs,
            json_file_count=len(all_entries),
            json_total_bytes=sum(entry["identity"][6] for entry in all_entries),
            relevant_file_count=len(all_entries),
            relevant_total_bytes=sum(entry["identity"][6] for entry in all_entries),
            parsed_file_count=len(all_entries),
            scan_elapsed_seconds=time.monotonic() - started,
            registry_path=path,
            registry_descriptor=descriptor,
            registry_identity=identity,
            registry_sha256=digest,
            generation=definition.generation,
            immutable_version=ROLLBACK_REGISTRY_IMMUTABLE_VERSION,
            entries=definition.active_entries,
            bootstrap_provenance=definition.historical_bootstrap,
            recent_refs=recent_refs,
            active_refs=active_refs,
            recent_entries=definition.recent_entries,
            evidence_path=binding["path"],
            evidence_descriptor=evidence_descriptor,
            evidence_identity=evidence_identity,
            evidence_sha256=evidence_digest,
            evidence_schema=binding["schema"],
            entry_set_digest_sha256=definition.entry_set_digest_sha256,
            inventory_digest_sha256=definition.inventory_digest_sha256,
            current_entry_counts=definition.current_entry_counts,
        )
    except BaseException:
        for retained_descriptor in (evidence_descriptor, descriptor):
            if retained_descriptor >= 0:
                try:
                    os.close(retained_descriptor)
                except OSError:
                    pass
        raise


def validate_active_rollback_index(
    index: ActiveRollbackIndex,
    *,
    registry_path: Path | None = None,
    root: Path | None = None,
    deadline: RunDeadline | None = None,
) -> None:
    """Revalidate only retained descriptors, named paths, and bounded members."""

    requested = registry_path or root
    if requested is not None and Path(requested) != index.registry_path:
        raise SiteAuthorityError("active_rollback_registry_path_drift")
    raw = _read_bound_registry_descriptor(index, deadline=deadline)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SiteAuthorityError("active_rollback_registry_malformed_during_validation") from exc
    definition = _validate_registry_payload(payload)
    if (
        definition.generation != index.generation
        or definition.entry_set_digest_sha256 != index.entry_set_digest_sha256
        or definition.inventory_digest_sha256 != index.inventory_digest_sha256
        or definition.evidence_binding["path"] != index.evidence_path
        or definition.evidence_binding["identity"] != index.evidence_identity
        or definition.evidence_binding["sha256"] != index.evidence_sha256
        or definition.active_entries != index.entries
        or definition.recent_entries != index.recent_entries
    ):
        raise SiteAuthorityError("active_rollback_registry_generation_drift")
    evidence_raw = _read_bound_generation_evidence_descriptor(index, deadline=deadline)
    try:
        evidence_payload = json.loads(evidence_raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SiteAuthorityError(
            "active_rollback_generation_evidence_malformed_during_validation"
        ) from exc
    _validate_generation_evidence_payload(evidence_payload, definition)
    active_refs: set[Path] = set()
    for entry in index.entries:
        active_refs.update(_validate_registered_entry(entry, deadline=deadline))
    recent_refs: set[Path] = set()
    now_ns = time.time_ns()
    for entry in index.recent_entries:
        recent_refs.update(
            _validate_registered_recent_entry(
                entry, deadline=deadline, now_ns=now_ns
            )
        )
    if active_refs != index.active_refs:
        raise SiteAuthorityError("active_rollback_registry_reference_set_drift")
    index.recent_refs = recent_refs
    index.refs = active_refs | recent_refs
    index.validation_count += 1


def active_rollback_references(
    *, deadline: RunDeadline | None = None
) -> set[Path]:
    index = build_active_rollback_index(deadline=deadline)
    try:
        return set(index.active_refs)
    finally:
        index.close()


def recent_mission_references(
    *,
    deadline: RunDeadline | None = None,
    rollback_index: ActiveRollbackIndex | None = None,
) -> set[Path]:
    """Return only canonical registry members; never discover under reports."""

    index = rollback_index
    owns_index = index is None
    if index is None:
        index = build_active_rollback_index(deadline=deadline)
    else:
        validate_active_rollback_index(index, deadline=deadline)
    try:
        return set(index.recent_refs)
    finally:
        if owns_index:
            index.close()


def _process_site_references(
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
) -> set[Path]:
    refs: set[Path] = set()
    for row in _process_rows(processes):
        for field in ("cmdline", "cwd", "environ", "exe", "root"):
            for match in SITE_REF_RE.findall(str(row.get(field, ""))):
                candidate = _safe_reference_from_text(match)
                if candidate is not None:
                    refs.add(candidate)
    if isinstance(processes, ProcessReferenceSnapshot):
        for value in processes.references:
            for match in SITE_REF_RE.findall(value):
                candidate = _safe_reference_from_text(match)
                if candidate is not None:
                    refs.add(candidate)
    return refs


def release_symlink_target_references(
    *, deadline: RunDeadline | None = None
) -> set[Path]:
    """Return every real tree reached by a release alias, or fail closed.

    Release aliases are non-canonical and therefore do not enter the
    current/rollback authority chain. Their real targets are nevertheless live
    dependencies: removing one leaves a dangling release path. Any ambiguity
    while reading that authority must suppress site archival rather than make
    the target look unreferenced.
    """

    def alias_identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    releases = FLAGSHIP / "releases"
    try:
        releases_before = releases.lstat()
        if stat.S_ISLNK(releases_before.st_mode) or not stat.S_ISDIR(
            releases_before.st_mode
        ):
            raise SiteAuthorityError("release_alias_root_directory_required")
        entries = sorted(releases.iterdir(), key=lambda item: item.name)
    except SiteAuthorityError:
        raise
    except OSError as exc:
        raise SiteAuthorityError(
            f"release_alias_root_unreadable:{type(exc).__name__}:{exc}"
        ) from exc
    lane_roots: set[Path] = set()
    for lane in SITE_LANE_RETENTION_SECONDS:
        lane_root = FLAGSHIP / lane
        try:
            lane_info = lane_root.lstat()
        except FileNotFoundError:
            # Optional retention lanes need not exist on this host. There is
            # no tree to protect there; an alias into it still fails strict
            # target resolution below. Never create a lane during inspection.
            continue
        except OSError as exc:
            raise SiteAuthorityError(
                f"release_alias_lane_root_unreadable:{lane_root}:{type(exc).__name__}:{exc}"
            ) from exc
        if stat.S_ISLNK(lane_info.st_mode) or not stat.S_ISDIR(lane_info.st_mode):
            raise SiteAuthorityError(
                f"release_alias_lane_root_directory_required:{lane_root}"
            )
        try:
            lane_roots.add(lane_root.resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise SiteAuthorityError(
                f"release_alias_lane_root_unreadable:{lane_root}:{type(exc).__name__}:{exc}"
            ) from exc
    refs: set[Path] = set()
    for index, entry in enumerate(entries):
        if index % 64 == 0:
            _check_deadline(deadline, "release_symlink_targets:scan")
        try:
            entry_before = entry.lstat()
        except OSError as exc:
            raise SiteAuthorityError(
                f"release_alias_entry_unreadable:{entry}:{type(exc).__name__}:{exc}"
            ) from exc
        if not stat.S_ISLNK(entry_before.st_mode):
            continue
        try:
            raw_target_before = os.readlink(entry)
            target = entry.resolve(strict=True)
            target_before = target.lstat()
        except (OSError, RuntimeError) as exc:
            raise SiteAuthorityError(
                f"release_alias_target_unreadable:{entry}:{type(exc).__name__}:{exc}"
            ) from exc
        if stat.S_ISLNK(target_before.st_mode) or not stat.S_ISDIR(
            target_before.st_mode
        ):
            raise SiteAuthorityError(
                f"release_alias_target_directory_required:{entry}:{target}"
            )
        if not any(under(target, root) for root in lane_roots):
            raise SiteAuthorityError(f"release_alias_target_escapes_lanes:{entry}:{target}")
        try:
            entry_after = entry.lstat()
            raw_target_after = os.readlink(entry)
            target_after = target.lstat()
        except OSError as exc:
            raise SiteAuthorityError(
                f"release_alias_identity_recheck_failed:{entry}:{type(exc).__name__}:{exc}"
            ) from exc
        if (
            alias_identity(entry_before) != alias_identity(entry_after)
            or raw_target_before != raw_target_after
        ):
            raise SiteAuthorityError(f"release_alias_identity_drift:{entry}")
        if alias_identity(target_before) != alias_identity(target_after):
            raise SiteAuthorityError(f"release_alias_target_identity_drift:{entry}:{target}")
        refs.add(target)
    try:
        releases_after = releases.lstat()
    except OSError as exc:
        raise SiteAuthorityError(
            f"release_alias_root_recheck_failed:{type(exc).__name__}:{exc}"
        ) from exc
    if alias_identity(releases_before) != alias_identity(releases_after):
        raise SiteAuthorityError("release_alias_root_identity_drift")
    return refs


def site_reference_context(
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    *,
    deadline: RunDeadline | None = None,
    rollback_index: ActiveRollbackIndex | None = None,
) -> dict[str, Any]:
    refs: set[Path] = {
        path.resolve(strict=False) for path in PROTECTED if under(path, FLAGSHIP)
    }
    authority: dict[str, Any] | None = None
    active_index = rollback_index
    authority_error: str | None = None
    supplemental_valid = True
    try:
        _check_deadline(deadline, "site_reference_context:canonical_authority")
        authority = load_site_tree_authority()
        refs.update(authority["refs"])
    except SiteAuthorityError as exc:
        authority_error = str(exc)
    try:
        if active_index is None:
            active_index = build_active_rollback_index(deadline=deadline)
        else:
            validate_active_rollback_index(active_index, deadline=deadline)
        refs.update(active_index.refs)
    except SiteAuthorityError as exc:
        supplemental_valid = False
        authority_error = (
            f"{authority_error};{exc}" if authority_error else str(exc)
        )
    try:
        refs.update(release_symlink_target_references(deadline=deadline))
    except SiteAuthorityError as exc:
        supplemental_valid = False
        authority_error = (
            f"{authority_error};{exc}" if authority_error else str(exc)
        )
    refs.update(_process_site_references(processes))
    valid = authority is not None and supplemental_valid and active_index is not None
    return {
        "site_authority_valid": valid,
        "site_authority_error": authority_error,
        "authority": authority,
        "active_rollback_index": active_index,
        "active_rollback_index_receipt": (
            active_index.receipt() if active_index is not None else None
        ),
        "refs": refs,
    }


def referenced_roots(
    *,
    processes: ProcessReferenceSnapshot | list[dict[str, Any]] | None = None,
    authority: dict[str, Any] | None = None,
    rollback_index: ActiveRollbackIndex | None = None,
    deadline: RunDeadline | None = None,
) -> set[Path]:
    process_rows = (
        (
            process_inventory(deadline=deadline)
            if deadline is not None
            else process_inventory()
        )
        if processes is None
        else processes
    )
    if authority is None:
        context = site_reference_context(
            process_rows,
            deadline=deadline,
            rollback_index=rollback_index,
        )
        if not context["site_authority_valid"]:
            raise SiteAuthorityError(str(context["site_authority_error"]))
        return set(context["refs"])
    refs: set[Path] = {
        path.resolve(strict=False) for path in PROTECTED if under(path, FLAGSHIP)
    }
    active_index = rollback_index or build_active_rollback_index(deadline=deadline)
    if rollback_index is not None:
        validate_active_rollback_index(active_index, deadline=deadline)
    refs.update(authority["refs"])
    refs.update(active_index.refs)
    refs.update(release_symlink_target_references(deadline=deadline))
    refs.update(_process_site_references(process_rows))
    return refs


def _reference_protects(path: Path, refs: set[Path]) -> bool:
    resolved = path.resolve(strict=False)
    return any(
        resolved == ref or under(resolved, ref) or under(ref, resolved) for ref in refs
    )


def candidate_retention_proof(path: Path) -> dict[str, Any]:
    """Require the only currently authoritative proof: an exact same-name release."""
    if path.name.startswith(PROTECTED_CANDIDATE_PREFIXES):
        return {
            "eligible": False,
            "status": "STATIC_CANDIDATE_KEEPER",
            "closed_lane_authority": "UNAVAILABLE_FAIL_CLOSED",
        }
    release = FLAGSHIP / "releases" / path.name
    try:
        release_stat = release.lstat()
    except OSError:
        return {
            "eligible": False,
            "status": "NO_SAME_NAME_RELEASE_PROOF",
            "closed_lane_authority": "UNAVAILABLE_FAIL_CLOSED",
        }
    if stat.S_ISLNK(release_stat.st_mode) or not stat.S_ISDIR(release_stat.st_mode):
        return {
            "eligible": False,
            "status": "SAME_NAME_RELEASE_INVALID",
            "closed_lane_authority": "UNAVAILABLE_FAIL_CLOSED",
        }
    if release.resolve(strict=False).parent != (FLAGSHIP / "releases").resolve(strict=False):
        return {
            "eligible": False,
            "status": "SAME_NAME_RELEASE_INVALID",
            "closed_lane_authority": "UNAVAILABLE_FAIL_CLOSED",
        }
    return {
        "eligible": True,
        "status": "EXACT_SAME_NAME_RELEASE_PROOF",
        "release_path": str(release.resolve(strict=False)),
        "closed_lane_authority": "UNAVAILABLE_NOT_USED",
    }


def candidate_kind(path: Path, refs: set[Path]) -> str | None:
    if excluded_from_scan(path) or protected(path):
        return None
    try:
        entry_stat = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(entry_stat.st_mode) or not (
        stat.S_ISREG(entry_stat.st_mode) or stat.S_ISDIR(entry_stat.st_mode)
    ):
        return None
    if _reference_protects(path, refs):
        return None
    resolved = path.resolve(strict=False)
    for bucket in SITE_LANE_RETENTION_SECONDS:
        root = FLAGSHIP / bucket
        if resolved.parent == root.resolve(strict=False):
            return "site_tree"
    if stat.S_ISDIR(entry_stat.st_mode) and resolved.parent == REPORTS.resolve(strict=False):
        return "report_tree"
    for root, kind in (
        (STATE_SNAPSHOTS, "state_snapshot"),
        (SESSION_SPILLS, "session_spill"),
        (CHECKPOINTS, "checkpoint_project"),
    ):
        if resolved.parent == root.resolve(strict=False):
            return kind
    return None


def _site_reason(lane: str) -> str:
    return {
        "releases": "release_tree_older_than_14d",
        "candidates": "candidate_tree_older_than_48h_with_same_name_release",
        "staging": "staging_tree_older_than_14d",
        "review-cuts": "review_cut_older_than_24h",
    }[lane]


def _snapshot_batch_member(
    path: Path,
    job_root: Path,
    *,
    deadline: RunDeadline | None = None,
) -> dict[str, Any]:
    _check_deadline(deadline, "cron_member_snapshot:start")
    try:
        first = path.lstat()
    except OSError as exc:
        raise SourceDriftError(f"cron_member_unavailable:{path}") from exc
    if stat.S_ISLNK(first.st_mode) or not stat.S_ISREG(first.st_mode):
        raise SourceDriftError(f"cron_member_regular_file_required:{path}")
    try:
        relative = path.relative_to(job_root)
        archive_path = path.relative_to(HOME)
    except ValueError as exc:
        raise SourceDriftError(f"cron_member_outside_root:{path}") from exc
    digest = sha256_file(path, deadline=deadline)
    try:
        second = path.lstat()
    except OSError as exc:
        raise SourceDriftError(f"cron_member_unavailable_after_hash:{path}") from exc
    if _identity_from_stat(first) != _identity_from_stat(second):
        raise SourceDriftError(f"cron_member_changed_during_hash:{path}")
    return {
        "path": str(path),
        "relative_path": str(relative),
        "archive_path": str(archive_path),
        "device": first.st_dev,
        "inode": first.st_ino,
        "type": "regular_file",
        "size": first.st_size,
        "mtime_ns": first.st_mtime_ns,
        "mode": first.st_mode,
        "uid": first.st_uid,
        "gid": first.st_gid,
        "nlink": first.st_nlink,
        "blocks": first.st_blocks,
        "allocated_bytes": first.st_blocks * 512,
        "sha256": digest,
    }


def _batch_manifest_sha(members: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(members, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def discover_cron_output_batches(
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    *,
    deadline: RunDeadline | None = None,
) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    try:
        job_roots = sorted(CRON_OUTPUT.iterdir(), key=lambda item: item.name)
    except OSError:
        return batches
    cutoff_ns = int((time.time() - CRON_OUTPUT_RETENTION_DAYS * 86400) * 1_000_000_000)
    for job_index, job_root in enumerate(job_roots):
        if job_index % 32 == 0:
            _check_deadline(deadline, "cron_discovery:jobs")
        try:
            job_stat = job_root.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(job_stat.st_mode) or not stat.S_ISDIR(job_stat.st_mode):
            continue
        if job_root.resolve(strict=False).parent != CRON_OUTPUT.resolve(strict=False):
            continue
        files: list[tuple[Path, os.stat_result]] = []
        for walk_index, (walk_root, dirs, names) in enumerate(
            os.walk(job_root, followlinks=False)
        ):
            if walk_index % 64 == 0:
                _check_deadline(deadline, "cron_discovery:walk")
            walk_path = Path(walk_root)
            dirs[:] = sorted(
                d
                for d in dirs
                if not (walk_path / d).is_symlink() and d != "node_modules"
            )
            for name in sorted(names):
                source = walk_path / name
                try:
                    source_stat = source.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(source_stat.st_mode) and not stat.S_ISLNK(source_stat.st_mode):
                    files.append((source, source_stat))
        if not files:
            continue
        newest_path, _ = max(files, key=lambda row: (row[1].st_mtime_ns, str(row[0])))
        eligible_rows = sorted(
            [
                (source, source_stat)
                for source, source_stat in files
                if source != newest_path
                and source_stat.st_mtime_ns < cutoff_ns
                and not path_is_active(source, processes, deep=True)
            ],
            key=lambda row: (row[1].st_mtime_ns, str(row[0])),
        )
        selected_paths = [
            source for source, _source_stat in eligible_rows[:CRON_BATCH_MEMBER_CAP]
        ]
        if not selected_paths:
            continue
        try:
            members = [
                _snapshot_batch_member(path, job_root, deadline=deadline)
                for path in sorted(selected_paths)
            ]
            retained_newest = _snapshot_batch_member(
                newest_path, job_root, deadline=deadline
            )
        except SourceDriftError:
            continue
        if any(member["mtime_ns"] >= cutoff_ns for member in members):
            continue
        inode_rows: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for member in members:
            inode_rows.setdefault((member["device"], member["inode"]), []).append(member)
        allocated = sum(rows[0]["allocated_bytes"] for rows in inode_rows.values())
        externally_linked = sum(
            rows[0]["allocated_bytes"]
            for rows in inode_rows.values()
            if rows[0]["nlink"] > len(rows)
        )
        batches.append(
            {
                "path": str(job_root),
                "kind": "cron_output_batch",
                "job_id": job_root.name,
                "reason": "cron_output_files_older_than_30d_preserving_newest_per_job",
                "candidate_class": "f13_bounded",
                "retained_newest_path": str(newest_path),
                "retained_newest": retained_newest,
                "members": members,
                "member_cap": CRON_BATCH_MEMBER_CAP,
                "eligible_member_count": len(eligible_rows),
                "selection_truncated": len(eligible_rows) > len(members),
                "stats": {
                    "files": len(members),
                    "logical_bytes": sum(member["size"] for member in members),
                    "allocated_bytes": allocated,
                    "externally_linked_allocated_bytes": externally_linked,
                    "reclaimable_allocated_bytes": max(0, allocated - externally_linked),
                    "hardlink_external_inode_count": sum(
                        1 for rows in inode_rows.values() if rows[0]["nlink"] > len(rows)
                    ),
                    "newest_mtime": max(member["mtime_ns"] for member in members) / 1e9,
                    "newest_path": max(members, key=lambda row: row["mtime_ns"])["path"],
                    "manifest_sha256": _batch_manifest_sha(members),
                    "retained_newest_manifest_sha256": _batch_manifest_sha(
                        [retained_newest]
                    ),
                },
            }
        )
    return batches


def eliminate_candidate_overlaps(
    candidates: list[dict[str, Any]],
    *,
    deadline: RunDeadline | None = None,
) -> list[dict[str, Any]]:
    """Eliminate duplicates/descendants in O(n log n + total path depth)."""

    by_parts: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, row in enumerate(candidates):
        if index % 256 == 0:
            _check_deadline(deadline, "candidate_overlap:index")
        parts = Path(row["path"]).resolve(strict=False).parts
        current = by_parts.get(parts)
        if current is None or int(row["stats"].get("allocated_bytes", 0)) > int(
            current["stats"].get("allocated_bytes", 0)
        ):
            by_parts[parts] = row

    kept_parts: set[tuple[str, ...]] = set()
    kept: list[dict[str, Any]] = []
    for index, (parts, row) in enumerate(
        sorted(by_parts.items(), key=lambda item: (len(item[0]), item[0]))
    ):
        if index % 256 == 0:
            _check_deadline(deadline, "candidate_overlap:select")
        if any(parts[:depth] in kept_parts for depth in range(1, len(parts))):
            continue
        kept_parts.add(parts)
        kept.append(row)
    return sorted(
        kept,
        key=lambda item: (
            -int(item["stats"].get("allocated_bytes", 0)),
            str(item["path"]),
        ),
    )


def discover_candidates(
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    refs: set[Path],
    *,
    site_authority_valid: bool = True,
    deadline: RunDeadline | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add_direct(
        path: Path,
        kind: str,
        retention_seconds: int,
        reason: str,
        minimum_bytes: int = 0,
        *,
        deep_activity: bool = True,
    ) -> None:
        _check_deadline(deadline, "candidate_discovery:add_direct")
        if candidate_kind(path, refs) != kind:
            return
        stats = fast_tree_stats(
            path,
            retention_seconds,
            minimum_bytes,
            deadline=deadline,
        )
        if stats is None or path_is_active(path, processes, deep=deep_activity):
            return
        candidates.append(
            {
                "path": str(path),
                "kind": kind,
                "reason": reason,
                "stats": stats,
                "candidate_class": "f13_bounded",
                "requires_site_authority": kind == "site_tree",
            }
        )

    # Only direct reports retain the 30-day plus 100 MiB gate.
    try:
        report_roots = sorted(REPORTS.iterdir(), key=lambda item: item.name)
    except OSError:
        report_roots = []
    for report_index, root_path in enumerate(report_roots):
        if report_index % 32 == 0:
            _check_deadline(deadline, "candidate_discovery:reports")
        add_direct(
            root_path,
            "report_tree",
            REPORT_RETENTION_DAYS * 86400,
            "report_over_100mb_older_than_30d",
            MIN_REPORT_ALLOCATED,
            deep_activity=True,
        )

    # Canonical authority failure suppresses every site lane but does not suppress
    # independently governed reports, state snapshots, cron outputs, or spills.
    if site_authority_valid:
        for lane, retention_seconds in SITE_LANE_RETENTION_SECONDS.items():
            if lane not in SITE_REMOVAL_LANES:
                continue
            root = FLAGSHIP / lane
            try:
                paths = sorted(root.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for path in paths:
                _check_deadline(deadline, f"candidate_discovery:site:{lane}")
                if lane == "candidates":
                    proof = candidate_retention_proof(path)
                    if not proof["eligible"]:
                        continue
                else:
                    proof = None
                add_direct(
                    path,
                    "site_tree",
                    retention_seconds,
                    _site_reason(lane),
                    0,
                    deep_activity=True,
                )
                if proof is not None and candidates and candidates[-1]["path"] == str(path):
                    candidates[-1]["retention_proof"] = proof
                    candidates[-1]["site_lane"] = lane
                elif candidates and candidates[-1]["path"] == str(path):
                    candidates[-1]["site_lane"] = lane

    for path in sorted(STATE_SNAPSHOTS.iterdir()) if STATE_SNAPSHOTS.exists() else ():
        add_direct(
            path,
            "state_snapshot",
            STATE_SNAPSHOT_RETENTION_DAYS * 86400,
            "state_snapshot_older_than_30d",
        )
    for path in sorted(SESSION_SPILLS.iterdir()) if SESSION_SPILLS.exists() else ():
        add_direct(
            path,
            "session_spill",
            SPILL_RETENTION_DAYS * 86400,
            "session_spill_older_than_14d",
        )
    for path in sorted(CHECKPOINTS.iterdir()) if CHECKPOINTS.exists() else ():
        add_direct(
            path,
            "checkpoint_project",
            CHECKPOINT_RETENTION_DAYS * 86400,
            "checkpoint_project_older_than_7d",
        )
    candidates.extend(
        discover_cron_output_batches(processes, deadline=deadline)
    )
    return eliminate_candidate_overlaps(candidates, deadline=deadline)


def cleanup_local_archive(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "attempted": False,
        "deleted": False,
        "status": "ALREADY_ABSENT",
    }
    if not (path.exists() or path.is_symlink()):
        result["deleted"] = True
        return result
    result["attempted"] = True
    try:
        path.unlink()
    except OSError as exc:
        result["status"] = "FAILED"
        result["error"] = f"{type(exc).__name__}:{exc}"[:500]
        return result
    result["deleted"] = not path.exists()
    result["status"] = "DELETED" if result["deleted"] else "FAILED"
    if not result["deleted"]:
        result["error"] = "archive_still_exists_after_unlink"
    return result


def _cleanup_partial_or_fail(path: Path, cause: BaseException) -> None:
    cleanup = cleanup_local_archive(path)
    if cleanup.get("deleted") is not True:
        raise RuntimeError(
            f"partial_archive_cleanup_failed:{path}:{cleanup.get('error', cleanup.get('status'))}"
        ) from cause


_SUBREAPER_INSTALLED = False


def install_descendant_reaper() -> None:
    """Make orphaned grandchildren reapable by this process on Linux."""

    global _SUBREAPER_INSTALLED
    if _SUBREAPER_INSTALLED:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_child_subreaper = 36
    if libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise RuntimeError(
            f"child_subreaper_install_failed:{error_number}:{os.strerror(error_number)}"
        )
    _SUBREAPER_INSTALLED = True


class ProcessTreeSupervisionError(RuntimeError):
    """A supervised process identity or bounded /proc proof became unsafe."""


@dataclass(frozen=True)
class _ProcRecord:
    pid: int
    ppid: int
    process_group: int
    session: int
    state: str
    start_time_ticks: int

    @property
    def key(self) -> tuple[int, int]:
        return self.pid, self.start_time_ticks


def _read_proc_record(pid: int) -> _ProcRecord | None:
    """Read one Linux process identity without treating ordinary exit as drift."""

    try:
        with open(f"/proc/{pid}/stat", "rb", buffering=0) as handle:
            raw = handle.read(16384)
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ProcessTreeSupervisionError(
            f"process_tree_proc_stat_unreadable:{pid}:{type(exc).__name__}:{exc}"
        ) from exc
    marker = raw.rfind(b") ")
    if marker <= 0:
        raise ProcessTreeSupervisionError(f"process_tree_proc_stat_malformed:{pid}")
    try:
        observed_pid = int(raw[: raw.find(b" ")])
        fields = raw[marker + 2 :].split()
        if len(fields) < 20:
            raise ValueError("short_stat")
        state = fields[0].decode("ascii")
        record = _ProcRecord(
            pid=observed_pid,
            ppid=int(fields[1]),
            process_group=int(fields[2]),
            session=int(fields[3]),
            state=state,
            start_time_ticks=int(fields[19]),
        )
    except (UnicodeError, ValueError) as exc:
        raise ProcessTreeSupervisionError(
            f"process_tree_proc_stat_malformed:{pid}:{type(exc).__name__}:{exc}"
        ) from exc
    if record.pid != pid or record.start_time_ticks <= 0:
        raise ProcessTreeSupervisionError(f"process_tree_proc_identity_invalid:{pid}")
    return record


def _self_task_ids() -> set[int]:
    try:
        task_ids = {
            int(entry.name)
            for entry in os.scandir("/proc/self/task")
            if entry.name.isdigit()
        }
    except OSError as exc:
        raise ProcessTreeSupervisionError(
            f"process_tree_self_tasks_unreadable:{type(exc).__name__}:{exc}"
        ) from exc
    if task_ids != {os.getpid()}:
        # A single invoking thread makes every novel direct child attributable
        # to the supervised spawn.  With another thread able to fork, that
        # attribution is unprovable, so fail before creating the child.
        raise ProcessTreeSupervisionError(
            f"process_tree_multithreaded_spawn_unprovable:{sorted(task_ids)}"
        )
    return task_ids


def _cleanup_root_proc_ambiguity_allowed(
    exc: ProcessTreeSupervisionError,
    expected_root_pid: int,
) -> bool:
    """Only the expected root may be omitted from cleanup-only /proc scans."""

    reason = str(exc)
    return reason.startswith(
        (
            f"process_tree_proc_stat_unreadable:{expected_root_pid}:",
            f"process_tree_proc_stat_malformed:{expected_root_pid}",
            f"process_tree_proc_identity_invalid:{expected_root_pid}",
        )
    )


def _proc_table_snapshot(
    *, cleanup_expected_root_pid: int | None = None
) -> dict[int, _ProcRecord]:
    try:
        pids = sorted(
            int(entry.name)
            for entry in os.scandir("/proc")
            if entry.name.isdigit()
        )
    except OSError as exc:
        raise ProcessTreeSupervisionError(
            f"process_tree_proc_root_unreadable:{type(exc).__name__}:{exc}"
        ) from exc
    if len(pids) > PROCESS_TREE_SCAN_MAX_PIDS:
        raise ProcessTreeSupervisionError(
            f"process_tree_proc_pid_cap_exceeded:{len(pids)}:{PROCESS_TREE_SCAN_MAX_PIDS}"
        )
    table: dict[int, _ProcRecord] = {}
    for pid in pids:
        try:
            record = _read_proc_record(pid)
        except ProcessTreeSupervisionError as exc:
            if (
                cleanup_expected_root_pid == pid
                and _cleanup_root_proc_ambiguity_allowed(exc, pid)
            ):
                continue
            raise
        if record is not None:
            table[pid] = record
    return table


@dataclass
class _ProcessTreeScope:
    """Identity-bound descendants created after one baseline child snapshot."""

    stage: str
    self_task_ids: set[int]
    baseline_keys: set[tuple[int, int]]
    run_context: RunContext | None
    start_time_floor: int
    root_key: tuple[int, int] | None = None
    cleanup_expected_root_pid: int | None = None
    cleanup_only: bool = False
    target_keys: set[tuple[int, int]] = field(default_factory=set)
    pidfds: dict[tuple[int, int], int] = field(default_factory=dict)
    term_signaled: set[tuple[int, int]] = field(default_factory=set)
    kill_signaled: set[tuple[int, int]] = field(default_factory=set)
    descendant_keys_counted: set[tuple[int, int]] = field(default_factory=set)
    detached_keys_counted: set[tuple[int, int]] = field(default_factory=set)
    closed: bool = False

    @classmethod
    def capture(
        cls,
        *,
        stage: str,
        run_context: RunContext | None,
    ) -> "_ProcessTreeScope":
        self_task_ids = _self_task_ids()
        table = _proc_table_snapshot()
        if run_context is not None:
            run_context.child_tree_scans += 1
        baseline_pids = {
            record.pid for record in table.values() if record.ppid in self_task_ids
        }
        changed = True
        while changed:
            changed = False
            for record in table.values():
                if record.pid not in baseline_pids and record.ppid in baseline_pids:
                    baseline_pids.add(record.pid)
                    changed = True
        return cls(
            stage=stage,
            self_task_ids=self_task_ids,
            baseline_keys={table[pid].key for pid in baseline_pids},
            run_context=run_context,
            start_time_floor=max(record.start_time_ticks for record in table.values()),
        )

    def _identity_failure(self, reason: str) -> ProcessTreeSupervisionError:
        if self.run_context is not None:
            self.run_context.child_tree_identity_failures += 1
        return ProcessTreeSupervisionError(f"{reason}:{self.stage}")

    def _pidfd_exited(self, identity: tuple[int, int]) -> bool:
        descriptor = self.pidfds.get(identity)
        if descriptor is None:
            return False
        try:
            readable, _, _ = select.select([descriptor], [], [], 0)
        except OSError as exc:
            raise self._identity_failure(
                f"process_tree_pidfd_poll_failed:{identity[0]}:{type(exc).__name__}:{exc}"
            ) from exc
        return bool(readable)

    def _open_pidfd(self, record: _ProcRecord) -> None:
        if record.key in self.pidfds:
            return
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            return
        try:
            descriptor = opener(record.pid, 0)
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.EINVAL):
                return
            if exc.errno == errno.ESRCH and _read_proc_record(record.pid) is None:
                return
            raise self._identity_failure(
                f"process_tree_pidfd_open_failed:{record.pid}:{exc.errno}"
            ) from exc
        try:
            after = _read_proc_record(record.pid)
        except BaseException:
            os.close(descriptor)
            raise
        if after is None or after.key != record.key:
            os.close(descriptor)
            if after is None:
                return
            raise self._identity_failure(
                f"process_tree_pidfd_identity_drift:{record.pid}"
            )
        self.pidfds[record.key] = descriptor
        if self.run_context is not None:
            self.run_context.child_tree_pidfds_opened += 1

    def _register(self, record: _ProcRecord) -> None:
        if record.start_time_ticks < self.start_time_floor:
            raise self._identity_failure(
                f"process_tree_target_before_spawn_floor:{record.pid}"
            )
        is_cleanup_root = bool(
            self.cleanup_only
            and self.cleanup_expected_root_pid is not None
            and record.pid == self.cleanup_expected_root_pid
        )
        if is_cleanup_root and self.root_key is None:
            self.root_key = record.key
        if record.key in self.target_keys:
            return
        self.target_keys.add(record.key)
        if is_cleanup_root:
            # The original process group remains authoritative for this root.
            # Avoid making cleanup depend on a second readable root /proc entry.
            return
        self._open_pidfd(record)
        if record.key == self.root_key or (
            self.root_key is None
            and record.pid == self.cleanup_expected_root_pid
        ):
            return
        if record.key not in self.descendant_keys_counted:
            self.descendant_keys_counted.add(record.key)
            if self.run_context is not None:
                self.run_context.child_tree_descendants_discovered += 1
        root_pid = (
            self.root_key[0]
            if self.root_key is not None
            else self.cleanup_expected_root_pid
        )
        if root_pid is None:
            raise self._identity_failure("process_tree_root_identity_missing")
        if (
            record.process_group != root_pid or record.session != root_pid
        ) and record.key not in self.detached_keys_counted:
            self.detached_keys_counted.add(record.key)
            if self.run_context is not None:
                self.run_context.child_tree_detached_discovered += 1

    def bind_root(self, pid: int) -> None:
        if self.root_key is not None:
            raise self._identity_failure("process_tree_root_already_bound")
        record = _read_proc_record(pid)
        if record is None:
            raise self._identity_failure(f"process_tree_root_missing_before_bind:{pid}")
        if record.key in self.baseline_keys or record.ppid not in self.self_task_ids:
            raise self._identity_failure(f"process_tree_root_parent_or_baseline_drift:{pid}")
        self.root_key = record.key
        self._register(record)
        if self.run_context is not None:
            self.run_context.child_trees_started += 1

    def bind_root_for_cleanup(self, expected_pid: int) -> None:
        """Establish cleanup attribution even when normal root binding failed.

        A readable exact root identity is retained when available.  If that one
        /proc record is unprovable, the freshly returned PID is used only as the
        root/group anchor while the pre-spawn floor and baseline identify every
        attributable descendant.  No other /proc ambiguity is relaxed.
        """

        if self.closed or expected_pid <= 0 or expected_pid in self.self_task_ids:
            raise self._identity_failure(
                f"process_tree_cleanup_root_invalid:{expected_pid}"
            )
        if (
            self.cleanup_expected_root_pid is not None
            and self.cleanup_expected_root_pid != expected_pid
        ):
            raise self._identity_failure(
                f"process_tree_cleanup_root_pid_drift:{expected_pid}"
            )
        if self.run_context is not None:
            self.run_context.child_tree_root_bind_failures += 1
        self.cleanup_expected_root_pid = expected_pid
        self.cleanup_only = True

        if self.root_key is not None:
            if (
                self.root_key[0] != expected_pid
                or self.root_key[1] < self.start_time_floor
                or self.root_key in self.baseline_keys
            ):
                raise self._identity_failure(
                    f"process_tree_cleanup_root_identity_unsafe:{expected_pid}"
                )
            self.target_keys.add(self.root_key)
        else:
            try:
                record = _read_proc_record(expected_pid)
            except ProcessTreeSupervisionError as exc:
                if not _cleanup_root_proc_ambiguity_allowed(exc, expected_pid):
                    raise
                record = None
            if record is not None:
                if (
                    record.start_time_ticks < self.start_time_floor
                    or record.key in self.baseline_keys
                ):
                    raise self._identity_failure(
                        f"process_tree_cleanup_root_identity_unsafe:{expected_pid}"
                    )
                self.root_key = record.key
                # Group cleanup handles the root; descendants receive pidfds.
                self.target_keys.add(record.key)
        if self.run_context is not None:
            self.run_context.child_tree_cleanup_root_bindings += 1

    def discover(self) -> list[_ProcRecord]:
        if self.closed or (
            self.root_key is None and self.cleanup_expected_root_pid is None
        ):
            raise self._identity_failure("process_tree_scope_not_open_or_bound")
        table = _proc_table_snapshot(
            cleanup_expected_root_pid=(
                self.cleanup_expected_root_pid if self.cleanup_only else None
            )
        )
        if self.run_context is not None:
            self.run_context.child_tree_scans += 1

        # Preserve the complete pre-spawn child family first.  Any descendants
        # it creates while supervision is active remain protected and can never
        # be mistaken for the supervised tree.
        protected_pids = {
            record.pid
            for record in table.values()
            if record.key in self.baseline_keys
        }
        changed = True
        while changed:
            changed = False
            for record in table.values():
                if record.pid not in protected_pids and record.ppid in protected_pids:
                    protected_pids.add(record.pid)
                    self.baseline_keys.add(record.key)
                    changed = True

        target_pids = {
            record.pid for record in table.values() if record.key in self.target_keys
        }
        if self.cleanup_expected_root_pid is not None:
            target_pids.add(self.cleanup_expected_root_pid)
        for record in table.values():
            if record.ppid not in self.self_task_ids or record.pid in protected_pids:
                continue
            if record.key in self.target_keys:
                target_pids.add(record.pid)
                continue
            if record.start_time_ticks < self.start_time_floor:
                raise self._identity_failure(
                    f"process_tree_unbound_preexisting_child:{record.pid}"
                )
            # With a single invoking thread and the complete baseline above,
            # every novel direct child is either the root or a subreaper-adopted
            # descendant of it, including setsid/double-fork escapes.
            target_pids.add(record.pid)

        changed = True
        while changed:
            changed = False
            for record in table.values():
                if (
                    record.pid not in target_pids
                    and record.pid not in protected_pids
                    and record.ppid in target_pids
                ):
                    if record.start_time_ticks < self.start_time_floor:
                        raise self._identity_failure(
                            f"process_tree_descendant_before_spawn_floor:{record.pid}"
                        )
                    target_pids.add(record.pid)
                    changed = True
        for pid in sorted(target_pids):
            record = table.get(pid)
            if record is not None:
                self._register(record)

        live: list[_ProcRecord] = []
        for identity in sorted(self.target_keys):
            record = table.get(identity[0])
            if record is not None and record.key == identity:
                live.append(record)
                continue
            if (
                record is None
                and self.cleanup_only
                and identity[0] == self.cleanup_expected_root_pid
            ):
                # The original group wait remains authoritative for this root.
                continue
            if record is None and identity in self.pidfds and not self._pidfd_exited(identity):
                raise self._identity_failure(
                    f"process_tree_proc_missing_for_live_pidfd:{identity[0]}"
                )
            # A reused PID is deliberately not inherited by the old binding.
        return live

    def reap_adopted_now(self, *, skip_pids: set[int]) -> int:
        reaped = 0
        for record in self.discover():
            if record.pid in skip_pids:
                continue
            try:
                waited, _status = os.waitpid(record.pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError):
                continue
            except InterruptedError:
                continue
            if waited == record.pid:
                reaped += 1
        if reaped and self.run_context is not None:
            self.run_context.child_tree_descendants_reaped += reaped
        return reaped

    def signal_live(self, sig: int) -> int:
        signaled = 0
        already = self.term_signaled if sig == signal.SIGTERM else self.kill_signaled
        sender = getattr(signal, "pidfd_send_signal", None)
        for record in self.discover():
            if record.state == "Z" or record.key in already:
                continue
            try:
                current = _read_proc_record(record.pid)
            except ProcessTreeSupervisionError as exc:
                if (
                    self.cleanup_only
                    and record.pid == self.cleanup_expected_root_pid
                    and _cleanup_root_proc_ambiguity_allowed(exc, record.pid)
                ):
                    # Group signaling/wait remains authoritative for this root.
                    continue
                raise
            if current is None:
                if record.key in self.pidfds and not self._pidfd_exited(record.key):
                    raise self._identity_failure(
                        f"process_tree_signal_proc_missing:{record.pid}"
                    )
                continue
            if current.key != record.key:
                continue
            try:
                descriptor = self.pidfds.get(record.key)
                if descriptor is not None and callable(sender):
                    sender(descriptor, sig, None, 0)
                else:
                    os.kill(record.pid, sig)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise self._identity_failure(
                    f"process_tree_signal_failed:{record.pid}:{sig}:{exc.errno}"
                ) from exc
            already.add(record.key)
            signaled += 1
        if self.run_context is not None:
            if sig == signal.SIGTERM:
                self.run_context.child_tree_term_signals += signaled
            elif sig == signal.SIGKILL:
                self.run_context.child_tree_kill_signals += signaled
        return signaled

    def mark_stable_empty(self) -> None:
        if self.run_context is not None:
            self.run_context.child_tree_stable_empty_proofs += 1

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for descriptor in self.pidfds.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.pidfds.clear()


def _process_tree_stable_empty(
    scope: _ProcessTreeScope,
    *,
    deadline: RunDeadline,
    skip_reap_pids: set[int],
) -> bool:
    """Return false on the first survivor; prove empty across repeated scans."""

    stable = 0
    while deadline.remaining() > 0:
        scope.reap_adopted_now(skip_pids=skip_reap_pids)
        if [
            record
            for record in scope.discover()
            if record.pid not in skip_reap_pids
        ]:
            return False
        stable += 1
        if stable >= PROCESS_TREE_STABLE_EMPTY_SCANS:
            scope.mark_stable_empty()
            return True
        time.sleep(min(PROCESS_TREE_STABLE_SCAN_SECONDS, deadline.remaining()))
    raise ProcessTreeSupervisionError(
        f"process_tree_stable_empty_deadline_exhausted:{scope.stage}"
    )


def _cleanup_process_tree(
    scope: _ProcessTreeScope,
    *,
    deadline: RunDeadline,
    skip_reap_pids: set[int],
) -> None:
    """Bounded TERM/KILL/reap waves with a repeated stable-empty proof."""

    scope.signal_live(signal.SIGTERM)
    term_end = min(
        deadline.deadline_monotonic,
        time.monotonic() + CHILD_TERM_GRACE_SECONDS,
    )
    stable = 0
    while deadline.remaining() > 0 and time.monotonic() < term_end:
        scope.reap_adopted_now(skip_pids=skip_reap_pids)
        live = [
            record
            for record in scope.discover()
            if record.pid not in skip_reap_pids
        ]
        if not live:
            stable += 1
            if stable >= PROCESS_TREE_STABLE_EMPTY_SCANS:
                scope.mark_stable_empty()
                return
        else:
            stable = 0
            scope.signal_live(signal.SIGTERM)
        time.sleep(
            min(
                PROCESS_TREE_STABLE_SCAN_SECONDS,
                deadline.remaining(),
                max(0.0, term_end - time.monotonic()),
            )
        )

    scope.signal_live(signal.SIGKILL)
    stable = 0
    while deadline.remaining() > 0:
        scope.reap_adopted_now(skip_pids=skip_reap_pids)
        live = [
            record
            for record in scope.discover()
            if record.pid not in skip_reap_pids
        ]
        if not live:
            stable += 1
            if stable >= PROCESS_TREE_STABLE_EMPTY_SCANS:
                scope.mark_stable_empty()
                return
        else:
            stable = 0
            scope.signal_live(signal.SIGKILL)
        time.sleep(min(PROCESS_TREE_STABLE_SCAN_SECONDS, deadline.remaining()))
    raise ProcessTreeSupervisionError(
        f"process_tree_survived_cleanup:{scope.stage}:"
        f"{sorted(record.pid for record in scope.discover())}"
    )


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_deadline_for(
    *,
    run_context: RunContext | None,
    operation_deadline: RunDeadline | None,
    stage: str,
) -> RunDeadline:
    source = run_context.deadline if run_context is not None else operation_deadline
    if source is not None and hasattr(source, "cleanup_deadline"):
        return source.cleanup_deadline(stage)
    # Compatibility for focused synthetic deadline fixtures.  Production always
    # supplies the run-level deadline above.
    return RunDeadline.start(max(1.0, CHILD_TERM_GRACE_SECONDS * 4))


def _terminal_deadline_for(deadline: Any, stage: str) -> RunDeadline:
    """Derive the reserved terminal-I/O envelope from any deadline fixture.

    Production ``RunDeadline`` provides ``terminal_deadline`` directly.  A
    focused deadline may mark ordinary work exhausted without implementing that
    convenience method; its monotonic envelope still defines the same hard
    scheduler bound and must remain usable for a nonblank terminal result.
    """

    terminal_factory = getattr(deadline, "terminal_deadline", None)
    if callable(terminal_factory):
        return terminal_factory(stage)
    now = time.monotonic()
    started = float(getattr(deadline, "started_monotonic", now))
    ordinary_end = float(getattr(deadline, "deadline_monotonic", now))
    terminal_end = min(
        started + OUTER_TIMEOUT_SECONDS,
        ordinary_end + CLEANUP_RECEIPT_MARGIN_SECONDS,
    ) - FINAL_OUTER_SAFETY_SLACK_SECONDS
    if terminal_end <= now:
        raise DeadlineExceeded(f"terminal_margin_exhausted:{stage}")
    return RunDeadline(now, terminal_end, terminal_end - now)


def _reap_group_children_now(process_group: int) -> int:
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-process_group, os.WNOHANG)
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if pid <= 0:
            break
        reaped += 1
    return reaped


def _reap_group_children(
    process_group: int,
    *,
    deadline: RunDeadline,
) -> tuple[int, bool]:
    reaped = 0
    leader_reaped = False
    while deadline.remaining() > 0:
        made_progress = False
        while True:
            try:
                pid, _status = os.waitpid(-process_group, os.WNOHANG)
            except ChildProcessError:
                pid = 0
            if pid <= 0:
                break
            made_progress = True
            reaped += 1
            leader_reaped = leader_reaped or pid == process_group
        if not _group_exists(process_group):
            try:
                pid, _status = os.waitpid(process_group, os.WNOHANG)
            except ChildProcessError:
                pid = process_group
            if pid == process_group:
                leader_reaped = True
            break
        if not made_progress:
            time.sleep(min(0.01, deadline.remaining()))
    return reaped, leader_reaped


def _terminate_popen_group(
    process: subprocess.Popen[str],
    *,
    run_context: RunContext | None,
    tree_scope: _ProcessTreeScope | None = None,
    cleanup_deadline: RunDeadline | None = None,
    operation_deadline: RunDeadline | None = None,
) -> tuple[str, str]:
    """TERM/KILL/reap and drain Popen pipes without a blocking fallback."""

    deadline = cleanup_deadline or _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=operation_deadline,
        stage="popen_group_cleanup",
    )
    process_group = process.pid
    chunks: dict[int, list[bytes]] = {}
    open_fds: dict[int, Any] = {}
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
        except (OSError, ValueError):
            continue
        open_fds[descriptor] = stream
        chunks[descriptor] = []

    def drain_once(wait: float) -> None:
        if not open_fds or deadline.remaining() <= 0:
            return
        readable, _, _ = select.select(
            list(open_fds), [], [], min(max(0.0, wait), deadline.remaining())
        )
        for descriptor in readable:
            while True:
                try:
                    payload = os.read(descriptor, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    payload = b""
                if not payload:
                    open_fds.pop(descriptor, None)
                    break
                chunks[descriptor].append(payload)
                if len(payload) < 65536:
                    break

    adopted_reaped = 0
    try:
        os.killpg(process_group, signal.SIGTERM)
        if run_context is not None:
            run_context.child_groups_terminated += 1
    except ProcessLookupError:
        pass
    term_end = min(
        deadline.deadline_monotonic,
        time.monotonic() + CHILD_TERM_GRACE_SECONDS,
    )
    while deadline.remaining() > 0 and time.monotonic() < term_end:
        process.poll()
        if process.returncode is not None:
            adopted_reaped += _reap_group_children_now(process_group)
        drain_once(0.01)
        if not _group_exists(process_group) and process.returncode is not None:
            break
    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
            if run_context is not None:
                run_context.child_groups_killed += 1
        except ProcessLookupError:
            pass
    if tree_scope is not None:
        _cleanup_process_tree(
            tree_scope,
            deadline=deadline,
            skip_reap_pids={process_group},
        )

    while deadline.remaining() > 0:
        process.poll()
        if process.returncode is not None:
            adopted_reaped += _reap_group_children_now(process_group)
        drain_once(0.01)
        if (
            not _group_exists(process_group)
            and process.returncode is not None
            and not open_fds
        ):
            break
        if not _group_exists(process_group) and process.returncode is not None:
            drain_once(0.0)
            if not open_fds:
                break
    stdout_fd = None
    stderr_fd = None
    try:
        stdout_fd = process.stdout.fileno() if process.stdout is not None else None
    except (OSError, ValueError):
        pass
    try:
        stderr_fd = process.stderr.fileno() if process.stderr is not None else None
    except (OSError, ValueError):
        pass
    stdout = (
        b"".join(chunks.get(stdout_fd, [])).decode("utf-8", errors="replace")
        if stdout_fd is not None
        else ""
    )
    stderr = (
        b"".join(chunks.get(stderr_fd, [])).decode("utf-8", errors="replace")
        if stderr_fd is not None
        else ""
    )
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    reaped, _leader_reaped = _reap_group_children(
        process_group,
        deadline=deadline,
    )
    if run_context is not None:
        run_context.child_groups_reaped += (
            int(process.returncode is not None) + adopted_reaped + reaped
        )
    if _group_exists(process_group) or process.returncode is None:
        raise RuntimeError(f"child_process_group_survived_cleanup:{process_group}")
    return stdout, stderr


def run_process_group(
    command: list[str],
    *,
    deadline: RunDeadline,
    cap_seconds: float,
    stage: str,
    run_context: RunContext | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one external command and prove its complete process tree empty."""

    install_descendant_reaper()
    timeout = deadline.timeout(f"{stage}:before_subprocess", cap_seconds)
    tree_scope = _ProcessTreeScope.capture(stage=stage, run_context=run_context)
    try:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    except BaseException:
        tree_scope.close()
        raise
    if run_context is not None:
        run_context.child_groups_started += 1
    try:
        try:
            tree_scope.bind_root(process.pid)
        except BaseException:
            tree_scope.bind_root_for_cleanup(process.pid)
            cleanup_deadline = _cleanup_deadline_for(
                run_context=run_context,
                operation_deadline=deadline,
                stage=f"{stage}:root_bind_cleanup",
            )
            _terminate_popen_group(
                process,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
            raise
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            cleanup_deadline = _cleanup_deadline_for(
                run_context=run_context,
                operation_deadline=deadline,
                stage=f"{stage}:timeout_cleanup",
            )
            _terminate_popen_group(
                process,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
            if deadline.remaining() <= 0.05:
                deadline.exhausted_stage = deadline.exhausted_stage or stage
                raise DeadlineExceeded(f"internal_deadline_exhausted:{stage}") from exc
            raise ArchivalAborted(f"child_process_timeout:{stage}:{timeout:.6f}") from exc
        except BaseException:
            cleanup_deadline = _cleanup_deadline_for(
                run_context=run_context,
                operation_deadline=deadline,
                stage=f"{stage}:exception_cleanup",
            )
            _terminate_popen_group(
                process,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
            raise
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=deadline,
            stage=f"{stage}:success_tree_proof",
        )
        tree_empty = _process_tree_stable_empty(
            tree_scope,
            deadline=cleanup_deadline,
            skip_reap_pids={process.pid},
        )
        if _group_exists(process.pid) or not tree_empty:
            _terminate_popen_group(
                process,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
            raise ProcessTreeSupervisionError(
                f"child_process_tree_not_empty_after_success:{stage}"
            )
        if run_context is not None:
            run_context.child_groups_reaped += 1
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        tree_scope.close()


def _terminate_fork_group(
    process_group: int,
    *,
    run_context: RunContext | None,
    tree_scope: _ProcessTreeScope | None = None,
    cleanup_deadline: RunDeadline | None = None,
    operation_deadline: RunDeadline | None = None,
) -> None:
    """Bounded TERM/KILL/WNOHANG cleanup; never block in waitpid."""

    deadline = cleanup_deadline or _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=operation_deadline,
        stage="fork_group_cleanup",
    )
    try:
        os.killpg(process_group, signal.SIGTERM)
        if run_context is not None:
            run_context.child_groups_terminated += 1
    except ProcessLookupError:
        pass
    reaped = 0
    leader_reaped = False
    term_end = min(
        deadline.deadline_monotonic,
        time.monotonic() + CHILD_TERM_GRACE_SECONDS,
    )
    while deadline.remaining() > 0 and time.monotonic() < term_end:
        try:
            pid, _status = os.waitpid(-process_group, os.WNOHANG)
        except ChildProcessError:
            pid = 0
        if pid > 0:
            reaped += 1
            leader_reaped = leader_reaped or pid == process_group
            continue
        if not _group_exists(process_group):
            break
        time.sleep(min(0.01, deadline.remaining(), max(0.0, term_end - time.monotonic())))
    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
            if run_context is not None:
                run_context.child_groups_killed += 1
        except ProcessLookupError:
            pass
    if tree_scope is not None:
        _cleanup_process_tree(
            tree_scope,
            deadline=deadline,
            skip_reap_pids={process_group},
        )
    more_reaped, more_leader_reaped = _reap_group_children(
        process_group,
        deadline=deadline,
    )
    reaped += more_reaped
    leader_reaped = leader_reaped or more_leader_reaped
    if not leader_reaped:
        try:
            pid, _status = os.waitpid(process_group, os.WNOHANG)
        except ChildProcessError:
            pid = process_group
        leader_reaped = pid == process_group
        if leader_reaped and pid > 0:
            reaped += 1
    if run_context is not None:
        run_context.child_groups_reaped += reaped
    if _group_exists(process_group) or not leader_reaped:
        raise RuntimeError(f"fork_process_group_survived_cleanup:{process_group}")


def _worker_emit(descriptor: int, value: dict[str, Any]) -> None:
    """Write one bounded, complete NDJSON worker frame."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > WORKER_FRAME_MAX_BYTES:
        raise RuntimeError(f"worker_frame_oversized:{len(payload)}")
    payload += b"\n"
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def _worker_test_hook(_stage: str, _detail: Any = None) -> None:
    """No-op seam used by hostile fork-worker regression fixtures."""


def _worker_error_reason(frame: dict[str, Any] | None, *, stage: str) -> str:
    if not isinstance(frame, dict):
        return f"worker_terminal_missing:{stage}"
    error_type = str(frame.get("error_type") or "RuntimeError")
    error = str(frame.get("error") or "worker_failed")[:1000]
    return f"worker_failed:{stage}:{error_type}:{error}"


def _write_worker_control(
    descriptor: int,
    payload: bytes,
    *,
    deadline: RunDeadline,
    stage: str,
) -> None:
    offset = 0
    while offset < len(payload):
        deadline.check(f"{stage}:control_write")
        _, writable, _ = select.select(
            [], [descriptor], [], min(WORKER_POLL_SECONDS, deadline.remaining())
        )
        if not writable:
            continue
        try:
            written = os.write(descriptor, payload[offset:])
        except BlockingIOError:
            continue
        if written <= 0:
            raise RuntimeError(f"worker_control_pipe_closed:{stage}")
        offset += written


def run_loaded_worker_bounded(
    worker: Callable[..., dict[str, Any]],
    *,
    run_context: RunContext | None,
    stage: str,
    cap_seconds: float,
    operation_deadline: RunDeadline | None = None,
    parent_monitor: Callable[[int, RunDeadline], None] | None = None,
    event_handler: Callable[[dict[str, Any], int, RunDeadline], bytes | None] | None = None,
    worker_control: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fork already-loaded/pinned code into a supervised process session.

    No helper pathname is imported or executed.  The parent drains progress,
    revalidates the held lock/canonical registry while the child runs, and on
    every timeout, signal, validation error, or malformed terminal frame sends
    TERM then KILL as needed and reaps the whole process group.
    """

    install_descendant_reaper()
    deadline = operation_deadline or (
        run_context.deadline if run_context is not None else RunDeadline.start(cap_seconds)
    )
    if run_context is not None:
        run_context.assert_worker_boundary(deadline=deadline)
    timeout = deadline.timeout(f"{stage}:before_worker", cap_seconds)
    tree_scope = _ProcessTreeScope.capture(stage=stage, run_context=run_context)
    read_fd, write_fd = os.pipe()
    control_read_fd = control_write_fd = -1
    if worker_control:
        control_read_fd, control_write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        if control_read_fd >= 0:
            os.close(control_read_fd)
        if control_write_fd >= 0:
            os.close(control_write_fd)
        tree_scope.close()
        raise
    if pid == 0:  # pragma: no cover - parent-observed hostile tests cover this
        exit_code = 0
        try:
            os.close(read_fd)
            if control_write_fd >= 0:
                os.close(control_write_fd)
            os.setsid()
            child_started = time.monotonic()
            child_deadline = RunDeadline(
                child_started,
                child_started + timeout,
                timeout,
            )
            try:
                result = (
                    worker(write_fd, control_read_fd, child_deadline)
                    if worker_control
                    else worker(write_fd, child_deadline)
                )
                child_deadline.check(f"{stage}:worker_terminal")
                _worker_emit(
                    write_fd,
                    {"event": "terminal", "ok": True, "result": result},
                )
            except BaseException as exc:
                exit_code = 1
                try:
                    _worker_emit(
                        write_fd,
                        {
                            "event": "terminal",
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:1000],
                            "deadline_exhausted": isinstance(exc, DeadlineExceeded),
                        },
                    )
                except BaseException:
                    pass
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            if control_read_fd >= 0:
                try:
                    os.close(control_read_fd)
                except OSError:
                    pass
            os._exit(exit_code)

    try:
        tree_scope.bind_root(pid)
    except BaseException:
        tree_scope.bind_root_for_cleanup(pid)
        for descriptor in (read_fd, write_fd, control_read_fd, control_write_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=_cleanup_deadline_for(
                    run_context=run_context,
                    operation_deadline=deadline,
                    stage=f"{stage}:root_bind_cleanup",
                ),
            )
        finally:
            tree_scope.close()
        raise
    os.close(write_fd)
    if control_read_fd >= 0:
        os.close(control_read_fd)
    if control_write_fd >= 0:
        os.set_blocking(control_write_fd, False)
    if run_context is not None:
        run_context.child_groups_started += 1
    events: list[dict[str, Any]] = []
    terminal_frame: dict[str, Any] | None = None
    buffer = bytearray()
    leader_status: int | None = None
    pipe_eof = False
    worker_end = time.monotonic() + timeout
    next_safety_check = time.monotonic()
    cleanup_needed = False
    caught: BaseException | None = None
    try:
        while not (pipe_eof and leader_status is not None):
            now = time.monotonic()
            if now >= worker_end:
                cleanup_needed = True
                exhausted = deadline.remaining() <= WORKER_POLL_SECONDS
                caught = WorkerSupervisionError(
                    (
                        f"internal_deadline_exhausted:{stage}"
                        if exhausted
                        else f"child_process_timeout:{stage}:{timeout:.6f}"
                    ),
                    stage=stage,
                    events=events,
                    deadline_exhausted=exhausted,
                    process_group=pid,
                )
                break
            try:
                deadline.check(f"{stage}:parent_wait")
            except DeadlineExceeded as exc:
                cleanup_needed = True
                caught = WorkerSupervisionError(
                    str(exc),
                    stage=stage,
                    events=events,
                    deadline_exhausted=True,
                    process_group=pid,
                )
                break
            if now >= next_safety_check:
                tree_scope.discover()
                if run_context is not None:
                    run_context.assert_worker_boundary(deadline=deadline)
                if parent_monitor is not None:
                    parent_monitor(pid, deadline)
                next_safety_check = now + WORKER_SAFETY_REVALIDATE_SECONDS
            wait = min(WORKER_POLL_SECONDS, max(0.0, worker_end - now))
            readable, _, _ = select.select([read_fd], [], [], wait)
            if readable:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    pipe_eof = True
                else:
                    buffer.extend(chunk)
                    if len(buffer) > WORKER_FRAME_MAX_BYTES * 4:
                        raise RuntimeError(f"worker_output_oversized:{stage}")
                    while b"\n" in buffer:
                        raw, _, remainder = buffer.partition(b"\n")
                        buffer = bytearray(remainder)
                        if not raw:
                            continue
                        try:
                            frame = json.loads(raw)
                        except (UnicodeError, json.JSONDecodeError) as exc:
                            raise RuntimeError(
                                f"worker_frame_invalid:{stage}:{type(exc).__name__}:{exc}"
                            ) from exc
                        if not isinstance(frame, dict):
                            raise RuntimeError(f"worker_frame_object_required:{stage}")
                        if frame.get("event") == "terminal":
                            if terminal_frame is not None:
                                raise RuntimeError(f"worker_terminal_duplicate:{stage}")
                            terminal_frame = frame
                        else:
                            events.append(frame)
                            if event_handler is not None:
                                response = event_handler(frame, pid, deadline)
                                if response:
                                    if control_write_fd < 0:
                                        raise RuntimeError(
                                            f"worker_control_not_enabled:{stage}"
                                        )
                                    _write_worker_control(
                                        control_write_fd,
                                        response,
                                        deadline=deadline,
                                        stage=stage,
                                    )
            if leader_status is None:
                waited, status_value = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    leader_status = status_value
        if caught is None and buffer:
            raise RuntimeError(f"worker_truncated_frame:{stage}")
    except BaseException as exc:
        cleanup_needed = True
        caught = exc
    finally:
        if control_write_fd >= 0:
            try:
                os.close(control_write_fd)
            except OSError:
                pass
            control_write_fd = -1
        if cleanup_needed:
            try:
                cleanup_deadline = _cleanup_deadline_for(
                    run_context=run_context,
                    operation_deadline=deadline,
                    stage=f"{stage}:worker_cleanup",
                )
                _terminate_fork_group(
                    pid,
                    run_context=run_context,
                    tree_scope=tree_scope,
                    cleanup_deadline=cleanup_deadline,
                )
                leader_status = leader_status if leader_status is not None else -1
            except BaseException as cleanup_exc:
                if caught is None:
                    caught = cleanup_exc
                else:
                    caught = RuntimeError(
                        f"{type(caught).__name__}:{caught};worker_cleanup:"
                        f"{type(cleanup_exc).__name__}:{cleanup_exc}"
                    )
        os.close(read_fd)

    if caught is not None:
        if isinstance(caught, WorkerSupervisionError):
            caught.events = events
            caught.process_group = pid
            tree_scope.close()
            raise caught
        tree_scope.close()
        raise WorkerSupervisionError(
            f"worker_supervision_failed:{stage}:{type(caught).__name__}:{caught}",
            stage=stage,
            events=events,
            deadline_exhausted=isinstance(caught, DeadlineExceeded),
            process_group=pid,
        ) from caught

    if leader_status is None:
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=deadline,
            stage=f"{stage}:missing_leader_cleanup",
        )
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            tree_scope.close()
        raise WorkerSupervisionError(
            f"worker_leader_not_reaped_before_terminal:{stage}",
            stage=stage,
            events=events,
            process_group=pid,
        )
    cleanup_deadline = _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=deadline,
        stage=f"{stage}:success_tree_proof",
    )
    try:
        tree_empty = _process_tree_stable_empty(
            tree_scope,
            deadline=cleanup_deadline,
            skip_reap_pids={pid},
        )
    except BaseException:
        tree_scope.close()
        raise
    if _group_exists(pid) or not tree_empty:
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            tree_scope.close()
        raise WorkerSupervisionError(
            f"worker_process_tree_not_empty_after_exit:{stage}",
            stage=stage,
            events=events,
            process_group=pid,
        )
    if run_context is not None:
        run_context.child_groups_reaped += 1
    if (
        terminal_frame is None
        or terminal_frame.get("ok") is not True
        or not os.WIFEXITED(leader_status)
        or os.WEXITSTATUS(leader_status) != 0
    ):
        reason = _worker_error_reason(terminal_frame, stage=stage)
        tree_scope.close()
        raise WorkerSupervisionError(
            reason,
            stage=stage,
            events=events,
            deadline_exhausted=bool(
                isinstance(terminal_frame, dict)
                and terminal_frame.get("deadline_exhausted") is True
            ),
            process_group=pid,
        )
    result = terminal_frame.get("result")
    if not isinstance(result, dict):
        tree_scope.close()
        raise WorkerSupervisionError(
            f"worker_result_object_required:{stage}",
            stage=stage,
            events=events,
            process_group=pid,
        )
    try:
        if run_context is not None:
            run_context.assert_worker_boundary(deadline=deadline)
        return result, events
    finally:
        tree_scope.close()


def run_r2_offload_bounded(
    archive: Path,
    archive_sha: str,
    *,
    run_context: RunContext,
    stage: str,
) -> dict[str, Any]:
    """Run the possibly blocking provider call in a killable process group."""

    run_context.assert_owned()
    install_descendant_reaper()
    timeout = run_context.deadline.timeout(f"{stage}:before_r2", R2_MAX_SECONDS)
    tree_scope = _ProcessTreeScope.capture(stage=stage, run_context=run_context)
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        tree_scope.close()
        raise
    if pid == 0:  # pragma: no cover - behavior is asserted from the parent
        try:
            os.close(read_fd)
            os.setsid()
            try:
                os.close(run_context.lock.descriptor)
            except OSError:
                pass
            try:
                result = r2_offload.offload(
                    archive,
                    archive_sha,
                    delete_local=False,
                )
            except BaseException as exc:
                result = {
                    "r2_status": "FAILED",
                    "verified": False,
                    "deleted_local": False,
                    "error": f"{type(exc).__name__}:{exc}"[:500],
                }
            payload = json.dumps(result, sort_keys=True).encode("utf-8")
            offset = 0
            while offset < len(payload):
                offset += os.write(write_fd, payload[offset:])
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(0)

    try:
        tree_scope.bind_root(pid)
    except BaseException:
        tree_scope.bind_root_for_cleanup(pid)
        os.close(read_fd)
        os.close(write_fd)
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=_cleanup_deadline_for(
                    run_context=run_context,
                    operation_deadline=run_context.deadline,
                    stage=f"{stage}:r2_root_bind_cleanup",
                ),
            )
        finally:
            tree_scope.close()
        raise
    os.close(write_fd)
    run_context.child_groups_started += 1
    run_context.effects["r2_calls"] += 1
    data = bytearray()
    end = time.monotonic() + timeout
    leader_status: int | None = None
    pipe_eof = False
    next_tree_check = time.monotonic()
    try:
        while not (pipe_eof and leader_status is not None):
            run_context.deadline.check(f"{stage}:r2_wait")
            if time.monotonic() >= next_tree_check:
                tree_scope.discover()
                next_tree_check = time.monotonic() + WORKER_SAFETY_REVALIDATE_SECONDS
            remaining = min(end - time.monotonic(), run_context.deadline.remaining())
            if remaining <= 0:
                raise DeadlineExceeded(f"internal_deadline_exhausted:{stage}:r2")
            readable, _, _ = select.select([read_fd], [], [], min(0.1, remaining))
            if readable:
                chunk = os.read(read_fd, 65536)
                if chunk:
                    data.extend(chunk)
                    if len(data) > WORKER_FRAME_MAX_BYTES:
                        raise RuntimeError("r2_worker_terminal_json_oversized")
                else:
                    pipe_eof = True
            if leader_status is None:
                waited, status_value = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    leader_status = status_value
                    run_context.child_groups_reaped += 1
    except BaseException:
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=run_context.deadline,
            stage=f"{stage}:r2_cleanup",
        )
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            tree_scope.close()
        raise
    finally:
        os.close(read_fd)
    if leader_status is None:
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=run_context.deadline,
            stage=f"{stage}:r2_missing_leader_cleanup",
        )
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            tree_scope.close()
        raise RuntimeError(f"r2_worker_leader_not_reaped:{pid}")
    cleanup_deadline = _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=run_context.deadline,
        stage=f"{stage}:r2_success_tree_proof",
    )
    try:
        tree_empty = _process_tree_stable_empty(
            tree_scope,
            deadline=cleanup_deadline,
            skip_reap_pids={pid},
        )
    except BaseException:
        tree_scope.close()
        raise
    if _group_exists(pid) or not tree_empty:
        try:
            _terminate_fork_group(
                pid,
                run_context=run_context,
                tree_scope=tree_scope,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            tree_scope.close()
        raise RuntimeError(f"r2_process_tree_not_empty_after_success:{pid}")
    tree_scope.close()
    try:
        value = json.loads(bytes(data))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"r2_worker_terminal_json_invalid:{type(exc).__name__}:{exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("r2_worker_terminal_json_object_required")
    return value


def _normalized_tar_name(value: str) -> str:
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def _verify_source_tar(
    archive: Path,
    source_relative: Path,
    before: dict[str, Any],
    content: dict[str, Any],
    *,
    deadline: RunDeadline | None = None,
) -> int:
    _check_deadline(deadline, "verify_source_tar:open")
    root_name = _normalized_tar_name(str(source_relative))
    expected_rows: dict[str, tuple[str, str | None]] = {}
    for rel, kind, value in content["rows"]:
        name = root_name if rel == "." else _normalized_tar_name(str(source_relative / rel))
        expected_rows[name] = (kind, value)
    seen: set[str] = set()
    member_count = 0
    with tarfile.open(archive, mode="r:gz") as tf:
        # Hardlink extraction may load the remaining headers into tf.members.
        # The iterator visits those cached members; tf.next() alone skips them.
        members = iter(tf)
        while True:
            _check_deadline(deadline, "verify_source_tar:next_member")
            _worker_test_hook("source_verify_before_member", member_count)
            info = next(members, None)
            if info is None:
                break
            member_count += 1
            if member_count > len(expected_rows):
                raise RuntimeError("archive_member_manifest_extra_member")
            name = _normalized_tar_name(info.name)
            if name in seen or name not in expected_rows:
                raise RuntimeError(f"archive_member_manifest_unexpected:{name}")
            seen.add(name)
            kind, value = expected_rows[name]
            if kind == "directory":
                if not info.isdir():
                    raise RuntimeError(f"archive_member_type_mismatch:{name}:directory")
            elif kind == "symlink":
                if not info.issym() or info.linkname != value:
                    raise RuntimeError(f"archive_member_symlink_mismatch:{name}")
            elif kind == "regular_file":
                if not (info.isfile() or info.islnk()):
                    raise RuntimeError(f"archive_member_type_mismatch:{name}:regular_file")
                extracted = tf.extractfile(info)
                if extracted is None:
                    raise RuntimeError(f"archive_member_unreadable:{name}")
                digest = hashlib.sha256()
                while True:
                    _check_deadline(deadline, "verify_source_tar:content")
                    _worker_test_hook("source_verify_content", name)
                    chunk = extracted.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != value:
                    raise RuntimeError(f"archive_member_content_mismatch:{name}")
            else:
                raise RuntimeError(f"archive_member_unsupported_type:{name}:{kind}")
    if seen != set(expected_rows):
        raise RuntimeError(
            "archive_member_manifest_mismatch:"
            f"expected={len(expected_rows)}:actual={len(seen)}"
        )
    if not before.get("manifest_sha256") or content.get("sha256") is None:
        raise RuntimeError("source_manifest_missing")
    return member_count


def _source_archive_verify_worker(
    archive: Path,
    source_relative: Path,
    before: dict[str, Any],
    content: dict[str, Any],
    _write_fd: int,
    deadline: RunDeadline,
) -> dict[str, Any]:
    member_count = _verify_source_tar(
        archive,
        source_relative,
        before,
        content,
        deadline=deadline,
    )
    _worker_test_hook("source_verify_before_digest", str(archive))
    archive_sha = sha256_file(archive, deadline=deadline)
    archive_stat = archive.lstat()
    if stat.S_ISLNK(archive_stat.st_mode) or not stat.S_ISREG(archive_stat.st_mode):
        raise RuntimeError("source_archive_regular_nonsymlink_required")
    deadline.check("source_verify:complete")
    return {
        "verified": True,
        "archive_sha256": archive_sha,
        "archive_size_bytes": archive_stat.st_size,
        "member_count": member_count,
    }


def _archive_digest_worker(
    archive: Path,
    _write_fd: int,
    deadline: RunDeadline,
) -> dict[str, Any]:
    first = archive.lstat()
    if stat.S_ISLNK(first.st_mode) or not stat.S_ISREG(first.st_mode):
        raise RuntimeError("local_archive_regular_nonsymlink_required")
    digest = _sha256_regular_unchanged(archive, first, deadline=deadline)
    final = archive.lstat()
    if _identity_from_stat(final) != _identity_from_stat(first):
        raise SourceDriftError("local_archive_identity_drift_during_digest")
    return {"sha256": digest, "bytes": final.st_size, "verified": True}


def _offhost_verified(
    r2: Any,
    archive: Path,
    archive_sha: str,
    *,
    deadline: RunDeadline | None = None,
    run_context: RunContext | None = None,
) -> bool:
    try:
        archive_size = archive.stat().st_size
        if run_context is not None:
            digest_result, _events = run_loaded_worker_bounded(
                lambda write_fd, worker_deadline: _archive_digest_worker(
                    archive, write_fd, worker_deadline
                ),
                run_context=run_context,
                stage="local_archive_digest_revalidation",
                cap_seconds=TAR_MAX_SECONDS,
                operation_deadline=deadline or run_context.deadline,
            )
            local_digest_matches = bool(
                digest_result.get("verified") is True
                and digest_result.get("sha256") == archive_sha
                and digest_result.get("bytes") == archive_size
            )
        else:
            local_digest_matches = sha256_file(archive, deadline=deadline) == archive_sha
    except (OSError, RuntimeError, ArchivalAborted):
        return False
    return bool(
        isinstance(r2, dict)
        and r2.get("r2_status") == "UPLOADED_VERIFIED"
        and r2.get("verified") is True
        and r2.get("sha256") == archive_sha
        and r2.get("bytes") == archive_size
        and r2.get("deleted_local") is False
        and archive.is_file()
        and local_digest_matches
    )


def _privileged_candidate_removal_required(
    source: Path,
    expected_stats: dict[str, Any],
) -> bool:
    """Select the privileged path only for foreign-owned candidate directories."""

    if source.parent != FLAGSHIP / "candidates":
        return False
    rows = expected_stats.get("manifest_rows")
    if not isinstance(rows, list):
        return False
    return any(
        len(row) >= 7 and row[1] == "directory" and int(row[5]) != os.geteuid()
        for row in rows
    )


def _retrieve_r2_archive_bounded(
    r2: dict[str, Any],
    archive_sha: str,
    archive_bytes: int,
    *,
    run_context: RunContext | None,
    deadline: RunDeadline | None,
) -> dict[str, Any]:
    """Fetch and hash the accepted archive before any privileged source effect."""

    bucket = r2.get("r2_bucket")
    key = r2.get("r2_key")
    if not isinstance(bucket, str) or not bucket or not isinstance(key, str) or not key:
        return {
            "retrievable": False,
            "expected_sha256": archive_sha,
            "expected_bytes": archive_bytes,
            "error": "r2_bucket_or_key_missing",
        }
    if run_context is None:
        return r2_offload.verify_retrievable(
            bucket=bucket,
            key=key,
            expected_sha256=archive_sha,
            expected_bytes=archive_bytes,
        )
    run_context.effects["r2_calls"] += 1
    result, _events = run_loaded_worker_bounded(
        lambda _write_fd, _worker_deadline: r2_offload.verify_retrievable(
            bucket=bucket,
            key=key,
            expected_sha256=archive_sha,
            expected_bytes=archive_bytes,
        ),
        run_context=run_context,
        stage="privileged_removal_r2_retrieval",
        cap_seconds=R2_MAX_SECONDS,
        operation_deadline=deadline or run_context.deadline,
    )
    return result


def _validate_privileged_helper() -> dict[str, Any]:
    try:
        before = PRIVILEGED_REMOVAL_HELPER.lstat()
    except OSError as exc:
        raise RuntimeError("privileged_removal_helper_missing") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != 0o755
        or before.st_nlink != 1
    ):
        raise RuntimeError("privileged_removal_helper_identity_invalid")
    digest = sha256_file(PRIVILEGED_REMOVAL_HELPER)
    after = PRIVILEGED_REMOVAL_HELPER.lstat()
    if _identity_from_stat(before) != _identity_from_stat(after):
        raise RuntimeError("privileged_removal_helper_identity_drift")
    if digest != EXPECTED_PRIVILEGED_REMOVAL_HELPER_SHA256:
        raise RuntimeError("privileged_removal_helper_sha256_mismatch")
    return {
        "path": str(PRIVILEGED_REMOVAL_HELPER),
        "sha256": digest,
        "uid": before.st_uid,
        "gid": before.st_gid,
        "mode": f"{stat.S_IMODE(before.st_mode):04o}",
        "nlink": before.st_nlink,
        "device": before.st_dev,
        "inode": before.st_ino,
    }


def _write_privileged_request(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("privileged_request_short_write")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _remove_privileged_candidate(
    source: Path,
    archive: Path,
    archive_sha: str,
    expected_stats: dict[str, Any],
    expected_content: dict[str, Any],
    retrieval: dict[str, Any],
    *,
    run_context: RunContext | None,
    deadline: RunDeadline | None,
) -> dict[str, Any]:
    """Invoke the root-owned candidate-only helper with an exact manifest request."""

    helper = _validate_privileged_helper()
    archive_value = archive.lstat()
    request = {
        "schema": PRIVILEGED_REMOVAL_REQUEST_SCHEMA,
        "source": str(source),
        "archive": str(archive),
        "archive_sha256": archive_sha,
        "archive_bytes": archive_value.st_size,
        "archive_created": True,
        "offhost_verified": True,
        "r2_retrieval": retrieval,
        "metadata_rows": expected_stats["manifest_rows"],
        "metadata_sha256": expected_stats["manifest_sha256"],
        "content_rows": expected_content["rows"],
        "content_sha256": expected_content["sha256"],
        "created_at_unix": time.time(),
    }
    request_path = archive.with_name(f"{archive.name}.privileged-removal-request.json")
    _write_privileged_request(request_path, request)
    free_before = _filesystem_free_bytes(source.parent)
    try:
        operation_deadline = deadline or RunDeadline.start(REMOVAL_MAX_SECONDS)
        proc = run_process_group(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/python3",
                "-I",
                "-S",
                str(PRIVILEGED_REMOVAL_HELPER),
                "--request",
                str(request_path),
            ],
            deadline=operation_deadline,
            cap_seconds=REMOVAL_MAX_SECONDS,
            stage="privileged_candidate_removal",
            run_context=run_context,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
        try:
            result = json.loads(proc.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("privileged_removal_result_json_invalid") from exc
        if not isinstance(result, dict) or result.get("schema") != PRIVILEGED_REMOVAL_RESULT_SCHEMA:
            raise RuntimeError("privileged_removal_result_schema_invalid")
        if (
            proc.returncode != 0
            or result.get("status") != "REMOVED"
            or result.get("source") != str(source)
            or result.get("source_removed") is not True
            or result.get("archive_sha256") != archive_sha
            or source.exists()
            or source.is_symlink()
        ):
            return {
                "removal_complete": False,
                "source_removed": not (source.exists() or source.is_symlink()),
                "source_retained": source.exists() or source.is_symlink(),
                "source_partially_removed": False,
                "source_state_uncertain": not (source.exists() or source.is_symlink()),
                "effects_uncertain": not (source.exists() or source.is_symlink()),
                "removed_entry_count": int(result.get("removed_entry_count") or 0),
                "removed_file_count": int(result.get("removed_file_count") or 0),
                "removed_directory_count": int(result.get("removed_directory_count") or 0),
                "removed_symlink_count": int(result.get("removed_symlink_count") or 0),
                "removed_logical_bytes": int(result.get("removed_logical_bytes") or 0),
                "removed_allocated_bytes_estimate": int(
                    result.get("removed_allocated_bytes_estimate") or 0
                ),
                "reclaimed_bytes": 0,
                "worker_error": (
                    f"privileged_removal_failed:returncode={proc.returncode}:"
                    f"{result.get('error') or proc.stderr[-1000:]}"[:1000]
                ),
                "worker_deadline_exhausted": False,
                "effect_ledger_event_count": 0,
                "privileged_removal": {
                    "helper": helper,
                    "result": result,
                    "request_path": str(request_path),
                },
            }
        if run_context is not None:
            run_context.effects["source_objects_removed"] += int(result["removed_entry_count"])
            run_context.effects["source_files_removed"] += int(result["removed_file_count"])
            run_context.effects["source_directories_removed"] += int(
                result["removed_directory_count"]
            )
        free_after = _filesystem_free_bytes(source.parent)
        return {
            "removal_complete": True,
            "source_removed": True,
            "source_retained": False,
            "source_partially_removed": False,
            "source_state_uncertain": False,
            "effects_uncertain": False,
            "removed_entry_count": int(result["removed_entry_count"]),
            "removed_file_count": int(result["removed_file_count"]),
            "removed_directory_count": int(result["removed_directory_count"]),
            "removed_symlink_count": int(result["removed_symlink_count"]),
            "removed_logical_bytes": int(result["removed_logical_bytes"]),
            "removed_allocated_bytes_estimate": int(
                result["removed_allocated_bytes_estimate"]
            ),
            "reclaimed_bytes": max(0, free_after - free_before),
            "reclaimed_bytes_uncertain": False,
            "worker_error": None,
            "worker_deadline_exhausted": False,
            "effect_ledger_event_count": 0,
            "privileged_removal": {
                "helper": helper,
                "result": result,
                "request_path": str(request_path),
            },
        }
    finally:
        request_path.unlink(missing_ok=True)
        parent_fd = os.open(request_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _privileged_release_reference_error(removal: dict[str, Any]) -> str | None:
    """Return a strict no-effect release-reference refusal from the root helper."""

    privileged = removal.get("privileged_removal")
    result = privileged.get("result") if isinstance(privileged, dict) else None
    error = result.get("error") if isinstance(result, dict) else None
    if (
        not isinstance(error, str)
        or result.get("status") != "BLOCKED"
        or removal.get("source_retained") is not True
        or removal.get("source_removed") is not False
        or removal.get("source_partially_removed") is not False
        or removal.get("source_state_uncertain") is not False
        or removal.get("effects_uncertain") is not False
        or int(removal.get("removed_entry_count") or 0) != 0
        or removal.get("worker_deadline_exhausted") is not False
    ):
        return None
    if error.startswith(PRIVILEGED_RELEASE_REFERENCE_ERROR_PREFIXES):
        return error
    return None


def _source_matches(
    source: Path,
    expected_identity: dict[str, Any],
    expected_stats: dict[str, Any],
    *,
    deadline: RunDeadline | None = None,
) -> bool:
    _check_deadline(deadline, "source_matches:metadata")
    try:
        current_identity = source_identity(source)
        current_stats = tree_stats(source, deadline=deadline)
    except (OSError, RuntimeError):
        return False
    metadata_matches = (
        current_identity == expected_identity
        and current_stats["manifest_sha256"] == expected_stats["manifest_sha256"]
        and current_stats["logical_bytes"] == expected_stats["logical_bytes"]
        and current_stats["allocated_bytes"] == expected_stats["allocated_bytes"]
    )
    if not metadata_matches:
        return False
    try:
        current_content = source_content_snapshot(source, deadline=deadline)
        final_identity = source_identity(source)
        final_stats = tree_stats(source, deadline=deadline)
    except (OSError, RuntimeError):
        return False
    return (
        current_content["sha256"] == expected_stats.get("content_manifest_sha256")
        and final_identity == expected_identity
        and final_stats["manifest_sha256"] == expected_stats["manifest_sha256"]
        and final_stats["logical_bytes"] == expected_stats["logical_bytes"]
        and final_stats["allocated_bytes"] == expected_stats["allocated_bytes"]
    )


def _filesystem_free_bytes(path: Path) -> int:
    value = os.statvfs(path)
    return value.f_bavail * value.f_frsize


def _record_effect_frame(
    frame: dict[str, Any],
    *,
    source_scope: str,
    run_context: RunContext | None,
    local_ledger: EffectLedger,
    provenance: str | None = None,
) -> bool:
    if frame.get("event") != "removal_completed":
        return False
    values = {
        "source_path": str(source_scope),
        "target_path": str(frame.get("path") or source_scope),
        "relative": str(frame.get("relative") or "."),
        "operation": str(frame.get("operation") or "unlink"),
        "kind": str(frame.get("kind") or "regular_file"),
        "logical_bytes": int(frame.get("size") or 0),
        "blocks": int(frame.get("blocks") or 0),
        "device": int(frame.get("device") or 0),
        "inode": int(frame.get("inode") or 0),
        "nlink": int(frame.get("nlink") or 1),
        "object_removed": bool(frame.get("object_removed", False)),
        "provenance": provenance or str(frame.get("provenance") or "worker_completed_frame"),
    }
    if run_context is not None:
        return run_context.record_removal_effect(**values)
    return local_ledger.record_removal(**values)


def _record_started_frame(
    frame: dict[str, Any],
    *,
    source_scope: str,
    ledger: EffectLedger,
) -> None:
    if frame.get("event") != "removal_started":
        return
    relative = str(frame.get("relative") or ".")
    operation = str(frame.get("operation") or "unlink")
    if any(
        row.get("event") == "removal_started"
        and row.get("source_path") == source_scope
        and row.get("relative") == relative
        and row.get("operation") == operation
        for row in ledger.events
    ):
        return
    ledger.events.append(
        {
            "event": "removal_started",
            "sequence": len(ledger.events) + 1,
            "source_path": source_scope,
            "target_path": str(frame.get("path") or relative),
            "relative": relative,
            "operation": operation,
            "kind": str(frame.get("kind") or "regular_file"),
            "provenance": str(frame.get("provenance") or "worker_started_frame"),
        }
    )


def _record_permission_frame(
    frame: dict[str, Any],
    *,
    source_scope: str,
    ledger: EffectLedger,
    provenance: str | None = None,
) -> bool:
    return ledger.record_permission_event(
        source_path=source_scope,
        frame=frame,
        provenance=provenance,
    )


def _regular_stat_matches_expected(
    value: os.stat_result,
    expected_identity: dict[str, Any],
) -> bool:
    """Compare every supplied regular-file identity field without following names."""

    actual = {
        **_identity_from_stat(value),
        "uid": value.st_uid,
        "gid": value.st_gid,
        "nlink": value.st_nlink,
        "blocks": getattr(value, "st_blocks", 0),
    }
    return bool(
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and all(
            key in actual and actual[key] == expected
            for key, expected in expected_identity.items()
        )
    )


def _remove_regular_unchanged(
    path: Path,
    expected_identity: dict[str, Any],
    expected_sha: str | None = None,
    *,
    deadline: RunDeadline | None = None,
    before_unlink: Callable[[], None] | None = None,
    effect_recorder: Callable[[dict[str, Any]], None] | None = None,
    source_scope: str | None = None,
    object_removed: bool = True,
) -> dict[str, Any]:
    """Descriptor-bind and unlink one file, preserving post-unlink truth."""

    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, parent_flags)
    descriptor = -1
    outcome: dict[str, Any] | None = None
    failure: BaseException | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        descriptor_stat = os.fstat(descriptor)
        if not _regular_stat_matches_expected(descriptor_stat, expected_identity):
            raise SourceDriftError(f"pre_remove_identity_drift:{path}")
        if (
            expected_sha is not None
            and _sha256_descriptor(descriptor, deadline=deadline) != expected_sha
        ):
            raise SourceDriftError(f"pre_remove_content_drift:{path}")
        _check_deadline(deadline, "remove_regular:before_unlink")
        entry_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (entry_stat.st_dev, entry_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise SourceDriftError(f"pre_remove_directory_entry_race:{path}")
        if before_unlink is not None:
            before_unlink()
        # Parent approval/current-reference refresh may block. Rebind both the
        # name and descriptor afterward so a same-path replacement (including
        # a symlink) can never inherit approval for the original inode.
        boundary = "post_approval" if before_unlink is not None else "final"
        _check_deadline(deadline, f"remove_regular:{boundary}_revalidation")
        descriptor_stat = os.fstat(descriptor)
        if not _regular_stat_matches_expected(descriptor_stat, expected_identity):
            raise SourceDriftError(f"{boundary}_descriptor_identity_drift:{path}")
        if (
            expected_sha is not None
            and _sha256_descriptor(descriptor, deadline=deadline) != expected_sha
        ):
            raise SourceDriftError(f"{boundary}_descriptor_content_drift:{path}")
        descriptor_stat = os.fstat(descriptor)
        if not _regular_stat_matches_expected(descriptor_stat, expected_identity):
            raise SourceDriftError(f"{boundary}_descriptor_identity_drift:{path}")
        try:
            entry_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise SourceDriftError(f"{boundary}_entry_unavailable_drift:{path}") from exc
        if not _regular_stat_matches_expected(entry_stat, expected_identity):
            raise SourceDriftError(f"{boundary}_entry_identity_drift:{path}")
        if (entry_stat.st_dev, entry_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise SourceDriftError(f"{boundary}_entry_descriptor_drift:{path}")
        _check_deadline(deadline, "remove_regular:immediately_before_unlink")
        os.unlink(path.name, dir_fd=parent_fd)
        outcome = {
            "path": str(path),
            "source_scope": source_scope or str(path),
            "source_removed": True,
            "source_retained": False,
            "source_partially_removed": False,
            "removal_complete": False,
            "durability_confirmed": False,
            "effects_uncertain": False,
            "removed_files": 1,
            "removed_directories": 0,
            "removed_logical_bytes": int(entry_stat.st_size),
            "device": int(entry_stat.st_dev),
            "inode": int(entry_stat.st_ino),
            "nlink": int(entry_stat.st_nlink),
            "blocks": int(getattr(entry_stat, "st_blocks", 0)),
        }
        if effect_recorder is not None:
            try:
                effect_recorder(
                    {
                        "event": "removal_completed",
                        "path": str(path),
                        "relative": ".",
                        "operation": "unlink",
                        "kind": "regular_file",
                        "size": int(entry_stat.st_size),
                        "blocks": int(getattr(entry_stat, "st_blocks", 0)),
                        "device": int(entry_stat.st_dev),
                        "inode": int(entry_stat.st_ino),
                        "nlink": int(entry_stat.st_nlink),
                        "object_removed": object_removed,
                        "provenance": "direct_syscall",
                    }
                )
            except BaseException as exc:
                outcome["effects_uncertain"] = True
                failure = PostUnlinkDurabilityError(
                    path=path,
                    outcome=outcome,
                    stage="effect_ledger_append",
                    cause=exc,
                )
        if failure is None:
            try:
                os.fsync(parent_fd)
            except BaseException as exc:
                failure = PostUnlinkDurabilityError(
                    path=path,
                    outcome=outcome,
                    stage="parent_fsync",
                    cause=exc,
                )
        if failure is None:
            outcome["durability_confirmed"] = True
            outcome["removal_complete"] = True
    finally:
        for close_stage, value in (("source_close", descriptor), ("parent_close", parent_fd)):
            if value < 0:
                continue
            try:
                os.close(value)
            except BaseException as exc:
                if outcome is not None and failure is None:
                    failure = PostUnlinkDurabilityError(
                        path=path,
                        outcome=outcome,
                        stage=close_stage,
                        cause=exc,
                    )
                elif outcome is None and failure is None:
                    failure = exc
    if failure is not None:
        raise failure
    if outcome is None:
        raise RuntimeError(f"regular_removal_outcome_missing:{path}")
    return outcome


def _open_directory_nofollow(path: Path) -> int:
    """Open every absolute path component with O_NOFOLLOW."""

    absolute = _canonical_absolute_path(path, label="removal_directory")
    if not absolute.is_absolute():
        raise SourceDriftError(f"directory_path_not_absolute:{path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise SourceDriftError(f"directory_path_component_invalid:{component!r}")
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_directory_nofollow(root_fd: int, relative: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.dup(root_fd)
    try:
        if relative not in {"", "."}:
            for component in Path(relative).parts:
                if component in {"", ".", ".."}:
                    raise SourceDriftError(
                        f"removal_relative_component_invalid:{relative!r}"
                    )
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _manifest_row_for_stat(
    relative: str,
    value: os.stat_result,
    *,
    symlink_target: str | None,
) -> tuple[Any, ...]:
    return (
        relative,
        _type_name(value.st_mode),
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
        getattr(value, "st_blocks", 0),
        symlink_target,
    )


def _descriptor_regular_sha256(
    descriptor: int,
    expected: os.stat_result,
    *,
    deadline: RunDeadline,
) -> str:
    opened = os.fstat(descriptor)
    if _identity_from_stat(opened) != _identity_from_stat(expected):
        raise SourceDriftError("descriptor_regular_identity_drift_before_hash")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        deadline.check("directory_removal:descriptor_hash")
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    final = os.fstat(descriptor)
    if _identity_from_stat(final) != _identity_from_stat(expected):
        raise SourceDriftError("descriptor_regular_identity_drift_during_hash")
    return digest.hexdigest()


def _descriptor_source_snapshot(
    root_fd: int,
    *,
    deadline: RunDeadline,
) -> tuple[list[tuple[Any, ...]], list[tuple[str, str, str | None]]]:
    """Inventory/hash a bound tree without following a single symlink."""

    metadata: list[tuple[Any, ...]] = []
    content: list[tuple[str, str, str | None]] = []
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SourceDriftError("bound_removal_root_not_directory")
    root_device = root_stat.st_dev

    def walk(directory_fd: int, relative: str) -> None:
        deadline.check("directory_removal:descriptor_inventory")
        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_dev != root_device:
            raise SourceDriftError(f"removal_cross_device_directory:{relative}")
        metadata.append(
            _manifest_row_for_stat(relative, directory_stat, symlink_target=None)
        )
        content.append((relative, "directory", None))
        names = sorted(os.listdir(directory_fd))
        for name in names:
            deadline.check("directory_removal:descriptor_inventory_entry")
            if name in {"", ".", ".."} or "/" in name:
                raise SourceDriftError(f"removal_entry_name_invalid:{name!r}")
            rel = name if relative == "." else f"{relative}/{name}"
            value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    if os.fstat(child_fd).st_ino != value.st_ino:
                        raise SourceDriftError(
                            f"removal_directory_identity_drift:{rel}"
                        )
                    walk(child_fd, rel)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(value.st_mode):
                file_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    metadata.append(
                        _manifest_row_for_stat(rel, value, symlink_target=None)
                    )
                    digest = _descriptor_regular_sha256(
                        file_fd, value, deadline=deadline
                    )
                    content.append((rel, "regular_file", digest))
                finally:
                    os.close(file_fd)
            elif stat.S_ISLNK(value.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                metadata.append(
                    _manifest_row_for_stat(rel, value, symlink_target=target)
                )
                content.append((rel, "symlink", target))
            else:
                raise SourceDriftError(
                    f"removal_unsupported_entry_type:{rel}:{oct(value.st_mode)}"
                )

    walk(root_fd, ".")
    return metadata, content


def _manifest_digest(rows: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _manifest_row_matches_stat(
    row: tuple[Any, ...],
    value: os.stat_result,
    *,
    exact: bool,
    removed_hardlink_count: int = 0,
) -> bool:
    stable = bool(
        row[1] == _type_name(value.st_mode)
        and row[2] == value.st_dev
        and row[3] == value.st_ino
        and row[4] == value.st_mode
        and row[5] == value.st_uid
        and row[6] == value.st_gid
    )
    if not stable or not exact:
        return stable
    return bool(
        row[7] == value.st_size
        and row[8] == value.st_mtime_ns
        and max(0, int(row[9]) - removed_hardlink_count) == value.st_nlink
        and row[10] == getattr(value, "st_blocks", 0)
    )


def _manifest_directory_identity_matches_except_mode(
    row: tuple[Any, ...], value: os.stat_result
) -> bool:
    return bool(
        row[1] == "directory"
        and stat.S_ISDIR(value.st_mode)
        and row[2] == value.st_dev
        and row[3] == value.st_ino
        and row[5] == value.st_uid
        and row[6] == value.st_gid
    )


def _assert_bound_root_entry(
    parent_fd: int,
    source_name: str,
    root_row: tuple[Any, ...],
    *,
    allowed_mode: int | None = None,
) -> None:
    try:
        entry = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise SourceDriftError("removal_root_path_missing_before_boundary") from exc
    identity_matches = _manifest_directory_identity_matches_except_mode(root_row, entry)
    mode_matches = entry.st_mode == root_row[4] or (
        allowed_mode is not None and stat.S_IMODE(entry.st_mode) == allowed_mode
    )
    if not identity_matches or not mode_matches:
        raise SourceDriftError("removal_root_path_identity_or_symlink_drift")


def _wait_directory_permission_approval(
    control_fd: int,
    *,
    deadline: RunDeadline,
    relative: str,
) -> None:
    while True:
        deadline.check(f"directory_permission:{relative}:approval_wait")
        readable, _, _ = select.select(
            [control_fd], [], [], min(WORKER_POLL_SECONDS, deadline.remaining())
        )
        if not readable:
            continue
        command = os.read(control_fd, 1)
        if command == b"U":
            return
        if not command:
            raise ArchivalAborted(
                f"directory_permission_approval_closed:{relative}"
            )
        raise ArchivalAborted(
            f"directory_permission_approval_invalid:{relative}:{command!r}"
        )


def _wait_directory_removal_approval(
    control_fd: int,
    *,
    deadline: RunDeadline,
    relative: str,
) -> None:
    """Wait for the parent's fresh active-reference gate for one delete effect."""

    while True:
        deadline.check(f"directory_removal:{relative}:approval_wait")
        readable, _, _ = select.select(
            [control_fd], [], [], min(WORKER_POLL_SECONDS, deadline.remaining())
        )
        if not readable:
            continue
        command = os.read(control_fd, 1)
        if command == b"U":
            return
        if not command:
            raise ArchivalAborted(f"directory_removal_approval_closed:{relative}")
        raise ArchivalAborted(
            f"directory_removal_approval_invalid:{relative}:{command!r}"
        )


def _ensure_directory_owner_write(
    directory_fd: int,
    relative: str,
    row: tuple[Any, ...],
    *,
    adjusted_modes: dict[str, int],
    write_fd: int,
    control_fd: int,
    deadline: RunDeadline,
) -> None:
    """Grant only owner-write on one frozen, descriptor-bound directory."""

    current = os.fstat(directory_fd)
    if not _manifest_directory_identity_matches_except_mode(row, current):
        raise SourceDriftError(
            f"permission_adjustment_directory_identity_drift:{relative}"
        )
    original_mode = stat.S_IMODE(int(row[4]))
    adjusted_mode = original_mode | stat.S_IWUSR
    current_mode = stat.S_IMODE(current.st_mode)
    if current_mode not in {original_mode, adjusted_mode}:
        raise SourceDriftError(
            f"permission_adjustment_directory_mode_drift:{relative}:"
            f"{current_mode:04o}"
        )
    if current_mode & stat.S_IWUSR:
        return
    if current.st_uid != os.geteuid():
        raise SourceDriftError(
            f"permission_adjustment_owner_mismatch:{relative}:"
            f"{current.st_uid}:{os.geteuid()}"
        )
    common = {
        "relative": relative,
        "mode_before": f"{original_mode:04o}",
        "mode_after": f"{adjusted_mode:04o}",
        "device": int(current.st_dev),
        "inode": int(current.st_ino),
        "uid": int(current.st_uid),
    }
    _worker_emit(
        write_fd,
        {
            "event": "permission_adjustment_requested",
            **common,
            "provenance": "descriptor_bound_owner_write_request",
        },
    )
    _wait_directory_permission_approval(
        control_fd, deadline=deadline, relative=relative
    )
    deadline.check("directory_removal:permission_adjustment_revalidation")
    revalidated = os.fstat(directory_fd)
    if (
        not _manifest_directory_identity_matches_except_mode(row, revalidated)
        or stat.S_IMODE(revalidated.st_mode) != original_mode
        or revalidated.st_uid != os.geteuid()
    ):
        raise SourceDriftError(
            f"permission_adjustment_post_approval_drift:{relative}"
        )
    _worker_test_hook("directory_removal_before_permission_adjustment", relative)
    deadline.check("directory_removal:immediately_before_permission_adjustment")
    immediately_before = os.fstat(directory_fd)
    if (
        not _manifest_directory_identity_matches_except_mode(row, immediately_before)
        or stat.S_IMODE(immediately_before.st_mode) != original_mode
        or immediately_before.st_uid != os.geteuid()
    ):
        raise SourceDriftError(
            f"permission_adjustment_immediate_boundary_drift:{relative}"
        )
    _worker_emit(
        write_fd,
        {
            "event": "permission_adjustment_started",
            **common,
            "provenance": "descriptor_bound_before_fchmod_frame",
        },
    )
    os.fchmod(directory_fd, adjusted_mode)
    _worker_emit(
        write_fd,
        {
            "event": "permission_adjustment_completed",
            **common,
            "provenance": "descriptor_bound_completed_fchmod_frame",
        },
    )
    adjusted = os.fstat(directory_fd)
    if (
        not _manifest_directory_identity_matches_except_mode(row, adjusted)
        or stat.S_IMODE(adjusted.st_mode) != adjusted_mode
    ):
        raise SourceDriftError(
            f"permission_adjustment_verification_failed:{relative}"
        )
    adjusted_modes[relative] = adjusted_mode
    _worker_test_hook("directory_removal_after_permission_adjustment", relative)


def _assert_bound_relative_directory(
    root_fd: int,
    relative: str,
    directory_fd: int,
    row: tuple[Any, ...],
    *,
    allowed_mode: int | None,
) -> None:
    """Prove a parent descriptor is still reachable at its frozen root-relative name."""

    if relative == ".":
        probe_fd = os.dup(root_fd)
    else:
        try:
            probe_fd = _open_relative_directory_nofollow(root_fd, relative)
        except OSError as exc:
            raise SourceDriftError(
                f"permission_adjustment_parent_path_drift:{relative}:"
                f"{type(exc).__name__}:{exc}"
            ) from exc
    try:
        descriptor_stat = os.fstat(directory_fd)
        probe_stat = os.fstat(probe_fd)
        mode_matches = probe_stat.st_mode == row[4] or (
            allowed_mode is not None
            and stat.S_IMODE(probe_stat.st_mode) == allowed_mode
        )
        if (
            not _manifest_directory_identity_matches_except_mode(row, descriptor_stat)
            or not _manifest_directory_identity_matches_except_mode(row, probe_stat)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (probe_stat.st_dev, probe_stat.st_ino)
            or not mode_matches
        ):
            raise SourceDriftError(
                f"permission_adjustment_parent_path_identity_drift:{relative}"
            )
    finally:
        os.close(probe_fd)


def _child_destructive_safety(
    run_context: RunContext | None,
    deadline: RunDeadline,
) -> None:
    deadline.check("directory_removal:destructive_boundary")
    if run_context is None:
        return
    run_context.lock.assert_owned(allow_inherited_worker=True)
    if run_context.active_rollback_index is not None:
        validate_active_rollback_index(
            run_context.active_rollback_index,
            deadline=deadline,
        )


def _remove_bound_directory_worker(
    parent_fd: int,
    root_fd: int,
    source_name: str,
    expected_metadata_rows: list[Any],
    expected_manifest_sha: str,
    expected_content_rows: list[Any],
    expected_content_sha: str,
    run_context: RunContext | None,
    write_fd: int,
    control_fd: int,
    deadline: RunDeadline,
) -> dict[str, Any]:
    expected_metadata = [tuple(row) for row in expected_metadata_rows]
    expected_content = [tuple(row) for row in expected_content_rows]
    if not expected_metadata or expected_metadata[0][0] != ".":
        raise SourceDriftError("removal_manifest_root_missing")
    root_row = expected_metadata[0]
    _worker_test_hook("directory_removal_before_preflight", source_name)
    _assert_bound_root_entry(parent_fd, source_name, root_row)
    actual_metadata, actual_content = _descriptor_source_snapshot(
        root_fd, deadline=deadline
    )
    if (
        actual_metadata != expected_metadata
        or _manifest_digest(actual_metadata) != expected_manifest_sha
    ):
        raise SourceDriftError("removal_metadata_manifest_drift_before_first_unlink")
    if (
        actual_content != expected_content
        or _manifest_digest(actual_content) != expected_content_sha
    ):
        raise SourceDriftError("removal_content_manifest_drift_before_first_unlink")
    _worker_emit(
        write_fd,
        {
            "event": "removal_preflight_verified",
            "entry_count": len(expected_metadata),
        },
    )

    content_map = {str(row[0]): tuple(row) for row in expected_content}
    removal_rows = sorted(
        expected_metadata[1:],
        key=lambda row: (str(row[0]).count("/"), str(row[0])),
        reverse=True,
    )
    removal_rows.append(root_row)
    completed = 0
    registry_validations = 0
    removed_inode_counts: dict[tuple[int, int], int] = {}
    adjusted_modes: dict[str, int] = {}
    metadata_by_relative = {str(row[0]): tuple(row) for row in expected_metadata}
    for row_value in removal_rows:
        row = tuple(row_value)
        relative = str(row[0])
        _worker_test_hook("directory_removal_before_entry", relative)
        _child_destructive_safety(run_context, deadline)
        if run_context is not None and run_context.active_rollback_index is not None:
            registry_validations += 1
        _assert_bound_root_entry(
            parent_fd,
            source_name,
            root_row,
            allowed_mode=adjusted_modes.get("."),
        )
        if relative == ".":
            parent_for_entry = os.dup(parent_fd)
            entry_name = source_name
            entry_fd = os.dup(root_fd)
        else:
            relative_path = Path(relative)
            parent_rel = str(relative_path.parent)
            parent_for_entry = _open_relative_directory_nofollow(root_fd, parent_rel)
            entry_name = relative_path.name
            entry_fd = -1
        try:
            if relative == ".":
                parent_relative = None
            else:
                parent_relative = str(Path(relative).parent) or "."
            if parent_relative is not None:
                parent_row = metadata_by_relative.get(parent_relative)
                if parent_row is None or parent_row[1] != "directory":
                    raise SourceDriftError(
                        f"permission_adjustment_parent_manifest_missing:{relative}"
                    )
                _ensure_directory_owner_write(
                    parent_for_entry,
                    parent_relative,
                    parent_row,
                    adjusted_modes=adjusted_modes,
                    write_fd=write_fd,
                    control_fd=control_fd,
                    deadline=deadline,
                )
                _assert_bound_relative_directory(
                    root_fd,
                    parent_relative,
                    parent_for_entry,
                    parent_row,
                    allowed_mode=adjusted_modes.get(parent_relative),
                )
            current = os.stat(
                entry_name, dir_fd=parent_for_entry, follow_symlinks=False
            )
            kind = str(row[1])
            if kind == "regular_file":
                removed_hardlinks = removed_inode_counts.get(
                    (int(row[2]), int(row[3])), 0
                )
                if not _manifest_row_matches_stat(
                    row,
                    current,
                    exact=True,
                    removed_hardlink_count=removed_hardlinks,
                ):
                    raise SourceDriftError(
                        f"removal_regular_identity_drift:{relative}"
                    )
                file_fd = os.open(
                    entry_name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_for_entry,
                )
                try:
                    content_row = content_map[relative]
                    if content_row[1] != "regular_file":
                        raise SourceDriftError(
                            f"removal_regular_content_kind_drift:{relative}"
                        )
                    if (
                        _descriptor_regular_sha256(
                            file_fd, current, deadline=deadline
                        )
                        != content_row[2]
                    ):
                        raise SourceDriftError(
                            f"removal_regular_content_drift:{relative}"
                        )
                finally:
                    os.close(file_fd)
                operation = "unlink"
            elif kind == "symlink":
                if not _manifest_row_matches_stat(row, current, exact=True):
                    raise SourceDriftError(
                        f"removal_symlink_identity_drift:{relative}"
                    )
                if os.readlink(entry_name, dir_fd=parent_for_entry) != row[11]:
                    raise SourceDriftError(
                        f"removal_symlink_target_drift:{relative}"
                    )
                operation = "unlink"
            elif kind == "directory":
                if entry_fd < 0:
                    entry_fd = os.open(
                        entry_name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_for_entry,
                    )
                directory_stat = os.fstat(entry_fd)
                directory_mode = adjusted_modes.get(relative)
                if (
                    not _manifest_directory_identity_matches_except_mode(
                        row, directory_stat
                    )
                    or (
                        directory_stat.st_mode != row[4]
                        and (
                            directory_mode is None
                            or stat.S_IMODE(directory_stat.st_mode) != directory_mode
                        )
                    )
                ):
                    raise SourceDriftError(
                        f"removal_directory_identity_drift:{relative}"
                    )
                if os.listdir(entry_fd):
                    raise SourceDriftError(
                        f"removal_directory_not_empty_or_added_entry:{relative}"
                    )
                operation = "rmdir"
            else:
                raise SourceDriftError(
                    f"removal_manifest_type_unsupported:{relative}:{kind}"
                )

            # The parent owns process/reference/test-fixture authority.  Ask it
            # for a fresh decision for *this* syscall while the candidate root
            # still exists.  This deliberately replaces the asynchronous
            # supervisor scan which could run after the final rmdir but before
            # worker reaping and falsely convert a completed removal into an
            # unreadable-candidate failure.
            _worker_emit(
                write_fd,
                {
                    "event": "removal_approval_requested",
                    "relative": relative,
                    "kind": kind,
                    "operation": operation,
                    "path": relative,
                    "provenance": "parent_fresh_reference_gate_request",
                },
            )
            _wait_directory_removal_approval(
                control_fd, deadline=deadline, relative=relative
            )

            # Approval is authority, not an identity lease.  Revalidate every
            # descriptor/path/content fact after approval and immediately before
            # publishing the started frame and executing the syscall.
            _child_destructive_safety(run_context, deadline)
            _assert_bound_root_entry(
                parent_fd,
                source_name,
                root_row,
                allowed_mode=adjusted_modes.get("."),
            )
            if parent_relative is not None:
                parent_row = metadata_by_relative[parent_relative]
                _assert_bound_relative_directory(
                    root_fd,
                    parent_relative,
                    parent_for_entry,
                    parent_row,
                    allowed_mode=adjusted_modes.get(parent_relative),
                )
            approved_current = os.stat(
                entry_name, dir_fd=parent_for_entry, follow_symlinks=False
            )
            if kind == "regular_file":
                removed_hardlinks = removed_inode_counts.get(
                    (int(row[2]), int(row[3])), 0
                )
                if not _manifest_row_matches_stat(
                    row,
                    approved_current,
                    exact=True,
                    removed_hardlink_count=removed_hardlinks,
                ):
                    raise SourceDriftError(
                        f"removal_regular_post_approval_identity_drift:{relative}"
                    )
                approved_fd = os.open(
                    entry_name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_for_entry,
                )
                try:
                    if (
                        _descriptor_regular_sha256(
                            approved_fd, approved_current, deadline=deadline
                        )
                        != content_map[relative][2]
                    ):
                        raise SourceDriftError(
                            f"removal_regular_post_approval_content_drift:{relative}"
                        )
                finally:
                    os.close(approved_fd)
            elif kind == "symlink":
                if (
                    not _manifest_row_matches_stat(row, approved_current, exact=True)
                    or os.readlink(entry_name, dir_fd=parent_for_entry) != row[11]
                ):
                    raise SourceDriftError(
                        f"removal_symlink_post_approval_drift:{relative}"
                    )
            else:
                approved_directory = os.fstat(entry_fd)
                directory_mode = adjusted_modes.get(relative)
                if (
                    not _manifest_directory_identity_matches_except_mode(
                        row, approved_current
                    )
                    or not _manifest_directory_identity_matches_except_mode(
                        row, approved_directory
                    )
                    or (approved_current.st_dev, approved_current.st_ino)
                    != (approved_directory.st_dev, approved_directory.st_ino)
                    or (
                        approved_current.st_mode != row[4]
                        and (
                            directory_mode is None
                            or stat.S_IMODE(approved_current.st_mode) != directory_mode
                        )
                    )
                    or os.listdir(entry_fd)
                ):
                    raise SourceDriftError(
                        f"removal_directory_post_approval_drift:{relative}"
                    )
            deadline.check("directory_removal:immediately_before_effect")
            _worker_emit(
                write_fd,
                {
                    "event": "removal_started",
                    "relative": relative,
                    "kind": kind,
                    "operation": operation,
                    "path": relative,
                },
            )
            _worker_test_hook("directory_removal_after_started", relative)
            if operation == "unlink":
                os.unlink(entry_name, dir_fd=parent_for_entry)
            else:
                os.rmdir(entry_name, dir_fd=parent_for_entry)
            # Commit the successful syscall frame before hooks, fsync, close,
            # or any other fallible post-step.
            _worker_emit(
                write_fd,
                {
                    "event": "removal_completed",
                    "relative": relative,
                    "kind": kind,
                    "operation": operation,
                    "path": relative,
                    "size": int(row[7]) if kind == "regular_file" else 0,
                    "logical_bytes": int(row[7]) if kind == "regular_file" else 0,
                    "device": int(row[2]),
                    "inode": int(row[3]),
                    "blocks": int(row[10]),
                    "nlink": int(row[9]),
                    "object_removed": relative == ".",
                    "provenance": "directory_worker_completed_frame",
                },
            )
            _worker_test_hook("directory_removal_after_effect", relative)
            try:
                os.fsync(parent_for_entry)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.EROFS}:
                    raise
            if kind == "regular_file":
                inode_key = (int(row[2]), int(row[3]))
                removed_inode_counts[inode_key] = removed_inode_counts.get(inode_key, 0) + 1
            completed += 1
            deadline.check("directory_removal:after_effect")
        finally:
            if entry_fd >= 0:
                os.close(entry_fd)
            os.close(parent_for_entry)
    return {
        "complete": True,
        "completed_entry_count": completed,
        "registry_validations": registry_validations,
        "permission_adjusted_directory_count": len(adjusted_modes),
    }


def _descriptor_relative_stat(
    root_fd: int,
    relative: str,
) -> os.stat_result | None:
    if relative == ".":
        return os.fstat(root_fd)
    path = Path(relative)
    parent_fd = _open_relative_directory_nofollow(root_fd, str(path.parent))
    try:
        return os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        os.close(parent_fd)


def _reconcile_directory_permissions(
    root_fd: int,
    expected_metadata_rows: list[Any],
    *,
    deadline: RunDeadline,
    ledger: EffectLedger,
    source: Path,
) -> dict[str, Any]:
    """Restore surviving owner-write adjustments under the cleanup deadline."""

    rows = [
        tuple(row)
        for row in expected_metadata_rows
        if tuple(row)[1] == "directory"
    ]
    scoped_events = ledger.events_for(str(source))
    started_relatives = {
        str(event.get("relative") or ".")
        for event in scoped_events
        if event.get("event") == "permission_adjustment_started"
    }
    completed_relatives = {
        str(event.get("relative") or ".")
        for event in scoped_events
        if event.get("event") == "permission_adjustment_completed"
    }
    restored: list[str] = []
    already_original: list[str] = []
    removed: list[str] = []
    uncertain: list[str] = []
    errors: list[str] = []
    for row in sorted(
        rows, key=lambda value: str(value[0]).count("/"), reverse=True
    ):
        relative = str(row[0])
        descriptor = -1
        try:
            deadline.check("directory_permission_reconciliation:open")
            if relative == ".":
                root_stat = os.fstat(root_fd)
                if root_stat.st_nlink == 0:
                    removed.append(relative)
                    continue
                descriptor = os.dup(root_fd)
            else:
                try:
                    descriptor = _open_relative_directory_nofollow(root_fd, relative)
                except FileNotFoundError:
                    removed.append(relative)
                    continue
            current = os.fstat(descriptor)
            if not _manifest_directory_identity_matches_except_mode(row, current):
                raise SourceDriftError(
                    f"permission_reconciliation_identity_drift:{relative}"
                )
            original_mode = stat.S_IMODE(int(row[4]))
            adjusted_mode = original_mode | stat.S_IWUSR
            current_mode = stat.S_IMODE(current.st_mode)
            if current_mode == original_mode:
                already_original.append(relative)
                continue
            if (
                current_mode != adjusted_mode
                or adjusted_mode == original_mode
                or relative not in started_relatives
            ):
                raise SourceDriftError(
                    f"permission_reconciliation_mode_or_lineage_drift:{relative}:"
                    f"{current_mode:04o}"
                )
            common = {
                "relative": relative,
                "mode_before": f"{original_mode:04o}",
                "mode_after": f"{adjusted_mode:04o}",
                "device": int(current.st_dev),
                "inode": int(current.st_ino),
                "uid": int(current.st_uid),
            }
            if relative not in completed_relatives:
                _record_permission_frame(
                    {
                        "event": "permission_adjustment_completed",
                        **common,
                    },
                    source_scope=str(source),
                    ledger=ledger,
                    provenance="descriptor_mode_reconciliation",
                )
                completed_relatives.add(relative)
            restore_common = {
                **common,
                "mode_before": f"{adjusted_mode:04o}",
                "mode_after": f"{original_mode:04o}",
            }
            deadline.check(
                "directory_permission_reconciliation:immediately_before_restore"
            )
            _record_permission_frame(
                {
                    "event": "permission_adjustment_restoration_started",
                    **restore_common,
                },
                source_scope=str(source),
                ledger=ledger,
                provenance="cleanup_descriptor_before_fchmod_frame",
            )
            os.fchmod(descriptor, original_mode)
            _record_permission_frame(
                {
                    "event": "permission_adjustment_restoration_completed",
                    **restore_common,
                },
                source_scope=str(source),
                ledger=ledger,
                provenance="cleanup_descriptor_completed_fchmod_frame",
            )
            restored_stat = os.fstat(descriptor)
            if (
                not _manifest_directory_identity_matches_except_mode(row, restored_stat)
                or stat.S_IMODE(restored_stat.st_mode) != original_mode
            ):
                raise SourceDriftError(
                    f"permission_reconciliation_restore_verification_failed:{relative}"
                )
            restored.append(relative)
        except BaseException as exc:
            uncertain.append(relative)
            errors.append(f"{relative}:{type(exc).__name__}:{exc}"[:1000])
            if isinstance(exc, DeadlineExceeded):
                remaining = (
                    {str(value[0]) for value in rows}
                    - set(restored)
                    - set(removed)
                    - set(already_original)
                )
                uncertain.extend(sorted(remaining - set(uncertain)))
                break
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    final_events = ledger.events_for(str(source))
    return {
        "schema": "f12-f13-directory-permission-reconciliation-v1",
        "minimum_scope_owner_write_only": True,
        "group_or_world_permission_grants": 0,
        "adjustment_requested_count": sum(
            event.get("event") == "permission_adjustment_requested"
            for event in final_events
        ),
        "adjustment_started_count": sum(
            event.get("event") == "permission_adjustment_started"
            for event in final_events
        ),
        "adjustment_completed_count": sum(
            event.get("event") == "permission_adjustment_completed"
            for event in final_events
        ),
        "restoration_started_count": sum(
            event.get("event") == "permission_adjustment_restoration_started"
            for event in final_events
        ),
        "restoration_completed_count": sum(
            event.get("event") == "permission_adjustment_restoration_completed"
            for event in final_events
        ),
        "restored_relatives": sorted(restored),
        "already_original_relatives": sorted(already_original),
        "removed_before_restore_relatives": sorted(removed),
        "uncertain_relatives": sorted(set(uncertain)),
        "errors": errors[:25],
        "permissions_reconciled": not uncertain,
        "permission_state_uncertain": bool(uncertain),
    }


def _reconcile_directory_removal(
    parent_fd: int,
    root_fd: int,
    source_name: str,
    expected_metadata_rows: list[Any],
    *,
    deadline: RunDeadline,
    events: list[dict[str, Any]],
    effect_recorder: Callable[[dict[str, Any]], None],
    source: Path,
) -> dict[str, Any]:
    rows = [tuple(row) for row in expected_metadata_rows]
    root_row = rows[0]
    rows_by_relative = {str(row[0]): row for row in rows}
    started = {
        str(event.get("relative"))
        for event in events
        if event.get("event") == "removal_started"
    }
    completed = {
        str(event.get("relative"))
        for event in events
        if event.get("event") == "removal_completed"
    }
    # Reconcile only started-without-completed effects first.  This is bounded
    # direct evidence and runs before the expensive whole-manifest pass.
    for relative in sorted(started - completed):
        deadline.check("directory_removal:reconcile_started_effect")
        row = rows_by_relative.get(relative)
        if row is None:
            raise SourceDriftError(
                f"directory_removal_started_event_unknown:{relative}"
            )
        try:
            current = (
                os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
                if relative == "."
                else _descriptor_relative_stat(root_fd, relative)
            )
        except (FileNotFoundError, NotADirectoryError):
            current = None
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                current = None
            else:
                raise
        if current is None:
            effect_recorder(
                {
                    "event": "removal_completed",
                    "relative": relative,
                    "kind": str(row[1]),
                    "operation": "rmdir" if row[1] == "directory" else "unlink",
                    "path": relative,
                    "size": int(row[7]) if row[1] == "regular_file" else 0,
                    "device": int(row[2]),
                    "inode": int(row[3]),
                    "blocks": int(row[10]),
                    "nlink": int(row[9]),
                    "object_removed": relative == ".",
                    "provenance": "bounded_started_effect_reconciliation",
                }
            )
    removed_rows: list[tuple[Any, ...]] = []
    drifted: list[str] = []
    try:
        root_entry = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        root_entry = None
    root_removed = root_entry is None
    if root_entry is not None and not _manifest_row_matches_stat(
        root_row, root_entry, exact=False
    ):
        drifted.append(".")
    current_by_relative: dict[str, os.stat_result | None] = {}
    for index, row in enumerate(rows):
        if index % 64 == 0:
            deadline.check("directory_removal:reconcile")
        relative = str(row[0])
        if relative == ".":
            current = None if root_removed else root_entry
        else:
            try:
                current = _descriptor_relative_stat(root_fd, relative)
            except (FileNotFoundError, NotADirectoryError):
                current = None
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                    current = None
                else:
                    raise
        current_by_relative[relative] = current
        if current is None:
            removed_rows.append(row)
            effect_recorder(
                {
                    "event": "removal_completed",
                    "relative": relative,
                    "kind": str(row[1]),
                    "operation": "rmdir" if row[1] == "directory" else "unlink",
                    "path": relative,
                    "size": int(row[7]) if row[1] == "regular_file" else 0,
                    "device": int(row[2]),
                    "inode": int(row[3]),
                    "blocks": int(row[10]),
                    "nlink": int(row[9]),
                    "object_removed": relative == ".",
                    "provenance": "full_manifest_reconciliation",
                }
            )

    removed_inode_counts: dict[tuple[int, int], int] = {}
    for removed_row in removed_rows:
        if removed_row[1] == "regular_file":
            inode_key = (int(removed_row[2]), int(removed_row[3]))
            removed_inode_counts[inode_key] = removed_inode_counts.get(inode_key, 0) + 1
    for row in rows:
        relative = str(row[0])
        current = current_by_relative[relative]
        if current is None:
            continue
        removed_hardlinks = (
            removed_inode_counts.get((int(row[2]), int(row[3])), 0)
            if row[1] == "regular_file"
            else 0
        )
        if not _manifest_row_matches_stat(
            row,
            current,
            exact=str(row[1]) != "directory",
            removed_hardlink_count=removed_hardlinks,
        ):
            drifted.append(relative)

    removed_rel = {str(row[0]) for row in removed_rows}
    inode_rows: dict[tuple[int, int], list[tuple[Any, ...]]] = {}
    for row in rows:
        if row[1] == "regular_file":
            inode_rows.setdefault((int(row[2]), int(row[3])), []).append(row)
    removed_allocated = 0
    for inode_group in inode_rows.values():
        if all(str(row[0]) in removed_rel for row in inode_group):
            expected_nlink = max(int(row[9]) for row in inode_group)
            if expected_nlink <= len(inode_group):
                removed_allocated += max(int(row[10]) for row in inode_group) * 512
    removed_file_count = sum(row[1] == "regular_file" for row in removed_rows)
    removed_directory_count = sum(row[1] == "directory" for row in removed_rows)
    removed_symlink_count = sum(row[1] == "symlink" for row in removed_rows)
    removed_logical = sum(
        int(row[7]) for row in removed_rows if row[1] == "regular_file"
    )
    physical_removed = len(removed_rows)
    removal_complete = bool(
        root_removed and physical_removed == len(rows) and not drifted
    )
    source_partially_removed = bool(physical_removed and not removal_complete)
    source_retained = bool(not physical_removed and not drifted and not root_removed)
    return {
        "removal_complete": removal_complete,
        "source_removed": removal_complete,
        "source_retained": source_retained,
        "source_partially_removed": source_partially_removed,
        "source_state_uncertain": bool(drifted),
        "effects_uncertain": bool(drifted),
        "drifted_entry_count": len(drifted),
        "drifted_entries": drifted[:25],
        "removed_entry_count": physical_removed,
        "removed_file_count": removed_file_count,
        "removed_directory_count": removed_directory_count,
        "removed_symlink_count": removed_symlink_count,
        "removed_logical_bytes": removed_logical,
        "removed_allocated_bytes_estimate": removed_allocated,
        "started_effect_count": len(started),
        "completed_effect_event_count": len(completed),
        "event_reconciliation_required": bool(started - completed),
    }


def _directory_lower_bound_reconciliation(
    source: Path,
    expected_metadata_rows: list[Any],
    *,
    events: list[dict[str, Any]],
    ledger: EffectLedger,
    error: BaseException,
) -> dict[str, Any]:
    removals = ledger.removals_for(str(source))
    started = {
        str(event.get("relative"))
        for event in events
        if event.get("event") == "removal_started"
    }
    completed = {
        str(event.get("relative"))
        for event in events
        if event.get("event") == "removal_completed"
    }
    ledger_relatives = {str(event.get("relative")) for event in removals}
    unresolved_started = started - completed - ledger_relatives
    root_effect = any(
        event.get("relative") == "." and event.get("operation") == "rmdir"
        for event in removals
    )
    removed_files = sum(event.get("kind") == "regular_file" for event in removals)
    removed_directories = sum(event.get("kind") == "directory" for event in removals)
    removed_symlinks = sum(event.get("kind") == "symlink" for event in removals)
    known_effect = bool(removals)
    possible_effect = bool(known_effect or started)
    return {
        "removal_complete": False,
        "source_removed": root_effect,
        "source_retained": not possible_effect,
        "source_partially_removed": bool(possible_effect and not root_effect),
        "source_state_uncertain": not root_effect and bool(unresolved_started),
        "effects_uncertain": True,
        "drifted_entry_count": 0,
        "drifted_entries": [],
        "removed_entry_count": len(removals),
        "removed_file_count": removed_files,
        "removed_directory_count": removed_directories,
        "removed_symlink_count": removed_symlinks,
        "removed_logical_bytes": sum(
            int(event.get("logical_bytes", 0)) for event in removals
        ),
        "removed_allocated_bytes_estimate": sum(
            int(event.get("allocated_bytes_delta", 0)) for event in removals
        ),
        "started_effect_count": len(started),
        "completed_effect_event_count": len(completed),
        "event_reconciliation_required": bool(unresolved_started),
        "unresolved_started_effect_count": len(unresolved_started),
        "expected_entry_count": len(expected_metadata_rows),
        "reconciliation_error": f"{type(error).__name__}:{error}"[:1000],
    }


def _remove_source_unchanged(
    source: Path,
    expected_identity: dict[str, Any],
    expected_stats: dict[str, Any],
    expected_content: dict[str, Any] | None = None,
    *,
    deadline: RunDeadline | None = None,
    run_context: RunContext | None = None,
) -> dict[str, Any]:
    if expected_identity.get("type") != "directory":
        raise SourceDriftError(f"directory_removal_type_required:{source}")
    try:
        _canonical_absolute_path(source, label="removal_source").relative_to(
            _canonical_absolute_path(HOME, label="removal_home")
        )
    except ValueError as exc:
        raise SourceDriftError(f"directory_removal_outside_home:{source}") from exc
    operation_deadline = deadline or RunDeadline.start(REMOVAL_MAX_SECONDS)
    operation_deadline.check("before_directory_removal_bind")
    if expected_content is None:
        expected_content = source_content_snapshot(source, deadline=operation_deadline)
    expected_rows = expected_stats.get("manifest_rows")
    if not isinstance(expected_rows, list):
        # tree_stats builds this exact in-memory manifest for normal callers.
        refreshed = tree_stats(source, deadline=operation_deadline)
        if refreshed.get("manifest_sha256") != expected_stats.get("manifest_sha256"):
            raise SourceDriftError(f"source_manifest_changed_before_removal:{source}")
        expected_rows = refreshed.get("manifest_rows")
    if not isinstance(expected_rows, list) or not expected_rows:
        raise SourceDriftError("directory_removal_manifest_rows_missing")
    parent_fd = _open_directory_nofollow(source.parent)
    root_fd = -1
    free_before = _filesystem_free_bytes(source.parent)
    events: list[dict[str, Any]] = []
    worker_error: BaseException | None = None
    worker_result: dict[str, Any] | None = None
    local_ledger = run_context.effect_ledger if run_context is not None else EffectLedger()

    def record_effect(frame: dict[str, Any]) -> None:
        _record_effect_frame(
            frame,
            source_scope=str(source),
            run_context=run_context,
            local_ledger=local_ledger,
        )

    def consume_worker_event(
        frame: dict[str, Any],
        _worker_pid: int,
        _monitor_deadline: RunDeadline,
    ) -> bytes | None:
        if frame.get("event") == "removal_approval_requested":
            if run_context is not None:
                run_context.destructive_reference_boundary(
                    source,
                    deadline=_monitor_deadline,
                    exclude_pids={os.getpid(), _worker_pid},
                    reuse_process_snapshot=True,
                    force_process_snapshot=frame.get("relative") == ".",
                )
            else:
                root_row = tuple(expected_rows[0])
                root_adjusted = any(
                    event.get("event") == "permission_adjustment_completed"
                    and event.get("source_path") == str(source)
                    and event.get("relative") == "."
                    for event in local_ledger.events
                )
                _assert_bound_root_entry(
                    parent_fd,
                    source.name,
                    root_row,
                    allowed_mode=(
                        stat.S_IMODE(int(root_row[4])) | stat.S_IWUSR
                        if root_adjusted
                        else None
                    ),
                )
            return b"U"
        if frame.get("event") == "removal_started":
            _record_started_frame(
                frame,
                source_scope=str(source),
                ledger=local_ledger,
            )
        elif frame.get("event") == "removal_completed":
            record_effect(frame)
        elif str(frame.get("event") or "").startswith("permission_adjustment_"):
            _record_permission_frame(
                frame,
                source_scope=str(source),
                ledger=local_ledger,
            )
            if frame.get("event") == "permission_adjustment_requested":
                if run_context is not None:
                    run_context.destructive_reference_boundary(
                        source,
                        deadline=_monitor_deadline,
                        exclude_pids={os.getpid(), _worker_pid},
                        reuse_process_snapshot=True,
                    )
                else:
                    root_row = tuple(expected_rows[0])
                    root_adjusted = any(
                        event.get("event") == "permission_adjustment_completed"
                        and event.get("source_path") == str(source)
                        and event.get("relative") == "."
                        for event in local_ledger.events
                    )
                    _assert_bound_root_entry(
                        parent_fd,
                        source.name,
                        root_row,
                        allowed_mode=(
                            stat.S_IMODE(int(root_row[4])) | stat.S_IWUSR
                            if root_adjusted
                            else None
                        ),
                    )
                return b"U"
        return None

    try:
        root_fd = os.open(
            source.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        root_row = tuple(expected_rows[0])
        _assert_bound_root_entry(parent_fd, source.name, root_row)
        if _identity_from_stat(os.fstat(root_fd)) != expected_identity:
            raise SourceDriftError(f"source_identity_changed_before_removal:{source}")
        if run_context is not None:
            run_context.assert_worker_boundary(deadline=operation_deadline)
        try:
            worker_result, events = _with_free_space_guard(
                lambda: run_loaded_worker_bounded(
                    lambda write_fd, control_fd, worker_deadline: _remove_bound_directory_worker(
                        parent_fd,
                        root_fd,
                        source.name,
                        expected_rows,
                        str(expected_stats["manifest_sha256"]),
                        list(expected_content["rows"]),
                        str(expected_content["sha256"]),
                        run_context,
                        write_fd,
                        control_fd,
                        worker_deadline,
                    ),
                    run_context=run_context,
                    stage="descriptor_safe_directory_removal",
                    cap_seconds=REMOVAL_MAX_SECONDS,
                    operation_deadline=operation_deadline,
                    # Periodic supervision still validates the lock, rollback
                    # registry and worker tree in run_loaded_worker_bounded.
                    # Active-reference authority is synchronized per effect by
                    # removal_approval_requested while the source exists.
                    parent_monitor=None,
                    event_handler=consume_worker_event,
                    worker_control=True,
                ),
                run_context=run_context,
            )
        except WorkerSupervisionError as exc:
            worker_error = exc
            events = list(exc.events)
        except BaseException as exc:
            worker_error = exc
        # Idempotently commit every completed frame before any path-wide
        # reconciliation.  The streaming handler normally already did this.
        for frame in events:
            if frame.get("event") == "removal_completed":
                record_effect(frame)
            elif str(frame.get("event") or "").startswith("permission_adjustment_"):
                _record_permission_frame(
                    frame,
                    source_scope=str(source),
                    ledger=local_ledger,
                )
        cleanup_source = (
            run_context.deadline if run_context is not None else operation_deadline
        )
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=cleanup_source,
            stage="directory_removal_reconcile",
        )
        permission_reconciliation = _reconcile_directory_permissions(
            root_fd,
            expected_rows,
            deadline=cleanup_deadline,
            ledger=local_ledger,
            source=source,
        )
        if permission_reconciliation["permission_state_uncertain"]:
            permission_reason = "directory_permission_reconciliation_uncertain"
            if run_context is not None:
                run_context.mark_effects_uncertain(str(source), permission_reason)
            else:
                local_ledger.mark_uncertain(str(source), permission_reason)
            worker_error = worker_error or RuntimeError(permission_reason)
        try:
            reconciliation = _reconcile_directory_removal(
                parent_fd,
                root_fd,
                source.name,
                expected_rows,
                deadline=cleanup_deadline,
                events=events,
                effect_recorder=record_effect,
                source=source,
            )
        except BaseException as reconciliation_error:
            if run_context is not None:
                run_context.mark_effects_uncertain(
                    str(source),
                    f"directory_reconciliation:{type(reconciliation_error).__name__}:"
                    f"{reconciliation_error}",
                )
            else:
                local_ledger.mark_uncertain(
                    str(source),
                    f"directory_reconciliation:{type(reconciliation_error).__name__}:"
                    f"{reconciliation_error}",
                )
            reconciliation = _directory_lower_bound_reconciliation(
                source,
                expected_rows,
                events=events,
                ledger=local_ledger,
                error=reconciliation_error,
            )
            worker_error = worker_error or reconciliation_error
        reconciliation["permission_reconciliation"] = permission_reconciliation
        if run_context is not None:
            try:
                run_context.assert_worker_boundary(deadline=cleanup_deadline)
            except BaseException as exc:
                worker_error = worker_error or exc
        if worker_result is not None and worker_result.get("complete") is not True:
            worker_error = RuntimeError("directory_removal_worker_nonterminal_result")
        if worker_error is None and not reconciliation["removal_complete"]:
            worker_error = RuntimeError("directory_removal_success_reconciliation_failed")
        if reconciliation.get("effects_uncertain"):
            if run_context is not None:
                run_context.mark_effects_uncertain(
                    str(source), "directory_reconciliation_reported_uncertain"
                )
            else:
                local_ledger.mark_uncertain(
                    str(source), "directory_reconciliation_reported_uncertain"
                )
        if worker_error is not None:
            reconciliation["worker_error"] = (
                f"{type(worker_error).__name__}:{worker_error}"[:1000]
            )
            reconciliation["worker_deadline_exhausted"] = bool(
                isinstance(worker_error, WorkerSupervisionError)
                and worker_error.deadline_exhausted
            )
            reconciliation["worker_process_group"] = (
                worker_error.process_group
                if isinstance(worker_error, WorkerSupervisionError)
                else None
            )
        else:
            reconciliation["worker_error"] = None
            reconciliation["worker_deadline_exhausted"] = False
            reconciliation["worker_process_group"] = None
        try:
            cleanup_deadline.check("directory_removal:before_free_space_sample")
            free_after = _filesystem_free_bytes(source.parent)
            reconciliation["reclaimed_bytes"] = max(0, free_after - free_before)
            reconciliation["reclaimed_bytes_uncertain"] = False
        except BaseException as free_error:
            reconciliation["reclaimed_bytes"] = 0
            reconciliation["reclaimed_bytes_uncertain"] = True
            reconciliation["post_removal_measurement_error"] = (
                f"{type(free_error).__name__}:{free_error}"[:1000]
            )
            if reconciliation.get("worker_error") is None:
                reconciliation["worker_error"] = (
                    f"post_removal_measurement:{type(free_error).__name__}:{free_error}"[:1000]
                )
        reconciliation["effect_ledger_event_count"] = len(
            local_ledger.removals_for(str(source))
        )
        reconciliation["effects_uncertain"] = bool(
            reconciliation.get("effects_uncertain")
            or str(source) in local_ledger.uncertain_scopes
        )
        return reconciliation
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def _site_removal_revalidation(
    row: dict[str, Any],
    source: Path,
    *,
    context: dict[str, Any] | None = None,
    deadline: RunDeadline | None = None,
) -> str | None:
    if not row.get("requires_site_authority"):
        return None
    if context is None:
        processes = process_inventory(deadline=deadline)
        context = site_reference_context(processes, deadline=deadline)
    if not context["site_authority_valid"]:
        return f"site_authority_invalid_before_removal:{context['site_authority_error']}"
    if _reference_protects(source, set(context["refs"])):
        return "site_reference_became_active_before_removal"
    if row.get("site_lane") == "candidates" and not candidate_retention_proof(source)["eligible"]:
        return "candidate_retention_proof_lost_before_removal"
    return None


def archive_one(
    row: dict[str, Any],
    archive_dir: Path,
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    *,
    run_context: RunContext | None = None,
) -> dict[str, Any]:
    source = Path(row["path"])
    deadline = run_context.deadline if run_context is not None else None
    if run_context is not None:
        run_context.assert_owned()
        before_checkpoint = run_context.checkpoint(
            "before_archive",
            require_site_context=bool(row.get("requires_site_authority")),
        )
        processes = before_checkpoint.processes
        authority_context = before_checkpoint.site_context
    else:
        authority_context = None
    _check_deadline(deadline, "candidate:before_archive")
    _active_test_boundary_hook("after_selection_before_archive", source)
    active_test_evidence = active_test_reference_gate(source)
    if active_test_evidence.get("protected") is True:
        return {
            "path": str(source),
            "kind": row.get("kind"),
            "status": "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            "reason": "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            "active_test_reference": active_test_evidence,
            "source_retained": source.exists() or source.is_symlink(),
            "source_removed": False,
            "archive_created": False,
            "r2_call_attempted": False,
            "reclaimed_bytes": 0,
            "offhost_verified_before_source_removal": False,
        }
    if path_is_active(source, processes, deep=True):
        return {
            "path": str(source),
            "kind": row.get("kind"),
            "status": "BLOCKED",
            "reason": "active_reference_before_archive",
            "source_retained": source.exists() or source.is_symlink(),
            "source_removed": False,
            "reclaimed_bytes": 0,
            "offhost_verified_before_source_removal": False,
        }
    authority_issue = _site_removal_revalidation(
        row,
        source,
        context=authority_context,
        deadline=deadline,
    )
    if authority_issue:
        return {
            "path": str(source),
            "kind": row.get("kind"),
            "status": "BLOCKED",
            "reason": authority_issue,
            "source_retained": source.exists() or source.is_symlink(),
            "source_removed": False,
            "reclaimed_bytes": 0,
            "offhost_verified_before_source_removal": False,
        }
    assert_free_floor("before_archive")
    before = tree_stats(source, deadline=deadline)
    if before["contains_node_modules"]:
        return {
            "path": str(source),
            "kind": row.get("kind"),
            "status": "SKIPPED",
            "reason": "contains_excluded_node_modules",
            "source_retained": True,
            "source_removed": False,
            "reclaimed_bytes": 0,
        }
    expected_identity = source_identity(source)
    content = source_content_snapshot(source, deadline=deadline)
    if source_identity(source) != expected_identity:
        raise SourceDriftError(f"source_identity_drift_during_content_manifest:{source}")
    before["content_manifest_sha256"] = content["sha256"]
    if expected_identity["type"] == "regular_file":
        before["single_file_sha256"] = content["rows"][0][2]
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        source_relative = source.absolute().relative_to(HOME.absolute())
    except ValueError as exc:
        raise RuntimeError(f"source_outside_home:{source}") from exc
    safe_name = str(source_relative).replace("/", "__")
    archive = archive_dir / f"{safe_name}-{stamp()}.tar.gz"
    partial = archive.with_suffix(archive.suffix + ".partial")
    if not safe_symlinks(source, deadline=deadline):
        return {"path": str(source), "status": "SKIPPED", "reason": "symlink_escapes_source"}
    try:
        tar_deadline = deadline or RunDeadline.start(TAR_MAX_SECONDS)
        tar_result = _with_free_space_guard(
            lambda: run_process_group(
                [
                    "/usr/bin/tar",
                    "--create",
                    "--use-compress-program=/usr/bin/pigz",
                    "--file",
                    str(partial),
                    "--directory",
                    str(HOME),
                    str(source_relative),
                ],
                deadline=tar_deadline,
                cap_seconds=TAR_MAX_SECONDS,
                stage="tar_archive",
                run_context=run_context,
            ),
            run_context=run_context,
        )
        if tar_result.returncode != 0:
            raise RuntimeError(f"tar_failed:{tar_result.stderr[-1000:]}")
    except BaseException as exc:
        _cleanup_partial_or_fail(partial, exc)
        raise
    try:
        os.chmod(partial, 0o600)
        verify_deadline = deadline or RunDeadline.start(TAR_MAX_SECONDS)
        verify_result, _verify_events = run_loaded_worker_bounded(
            lambda write_fd, worker_deadline: _source_archive_verify_worker(
                partial,
                source_relative,
                before,
                content,
                write_fd,
                worker_deadline,
            ),
            run_context=run_context,
            stage="source_archive_verify",
            cap_seconds=TAR_MAX_SECONDS,
            operation_deadline=verify_deadline,
        )
        partial_stat = partial.lstat()
        if (
            stat.S_ISLNK(partial_stat.st_mode)
            or not stat.S_ISREG(partial_stat.st_mode)
            or partial_stat.st_size != verify_result.get("archive_size_bytes")
            or verify_result.get("verified") is not True
        ):
            raise RuntimeError("source_archive_worker_result_or_identity_mismatch")
        archive_sha = str(verify_result["archive_sha256"])
        if not SHA256_RE.fullmatch(archive_sha):
            raise RuntimeError("source_archive_worker_digest_invalid")
        member_count = int(verify_result["member_count"])
        _check_deadline(deadline, "candidate:before_archive_commit")
        if run_context is not None:
            run_context.assert_worker_boundary(deadline=deadline)
        os.replace(partial, archive)
        if run_context is not None:
            run_context.effects["archive_files_created"] += 1
    except BaseException as exc:
        _cleanup_partial_or_fail(partial, exc)
        raise

    base_result: dict[str, Any] = {
        "path": str(source),
        "kind": row.get("kind"),
        "reason": row["reason"],
        "archive": str(archive),
        "archive_sha256": archive_sha,
        "archive_created": True,
        "r2_call_attempted": False,
        "archive_members": member_count,
        "source_files": before["files"],
        "source_logical_bytes": before["logical_bytes"],
        "source_allocated_bytes": before["allocated_bytes"],
        "source_externally_linked_allocated_bytes": before[
            "externally_linked_allocated_bytes"
        ],
        "source_reclaimable_allocated_bytes": before["reclaimable_allocated_bytes"],
        "source_manifest_sha256": before["manifest_sha256"],
        "source_content_manifest_sha256": before["content_manifest_sha256"],
        "source_identity": expected_identity,
    }
    if expected_identity["type"] == "regular_file":
        # This field is specifically the digest of one regular-file source.
        # Directory content is bound by source_content_manifest_sha256 instead;
        # omitting the inapplicable single-file field avoids fabricating a hash
        # or serializing a contradictory null value.
        base_result["source_content_sha256"] = before["single_file_sha256"]

    def blocked_result(
        reason: str,
        *,
        r2: dict[str, Any] | None = None,
        cleanup_if_source_retained: bool = True,
        status: str = "BLOCKED",
        active_test_reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_retained = source.exists() or source.is_symlink()
        if source_retained and cleanup_if_source_retained:
            cleanup = cleanup_local_archive(archive)
        else:
            cleanup = {
                "path": str(archive),
                "attempted": False,
                "deleted": False,
                "status": "RETAINED_FOR_RECOVERY",
            }
        result = {
            **base_result,
            "status": status,
            "reason": reason,
            "source_retained": source_retained,
            "source_removed": False,
            "reclaimed_bytes": 0,
            "offhost_verified_before_source_removal": False,
            "local_archive_cleanup": cleanup,
        }
        if r2 is not None:
            result["r2"] = r2
        if active_test_reference is not None:
            result["active_test_reference"] = active_test_reference
        return result

    try:
        assert_free_floor("after_archive_before_offload")
        if run_context is not None:
            after_archive_checkpoint = run_context.checkpoint(
                "before_r2",
                require_site_context=bool(row.get("requires_site_authority")),
            )
            after_archive_processes = after_archive_checkpoint.processes
            after_archive_site_context = after_archive_checkpoint.site_context
        else:
            after_archive_processes = process_inventory()
            after_archive_site_context = None
    except BaseException:
        cleanup_local_archive(archive)
        raise
    if not _source_matches(
        source,
        expected_identity,
        before,
        deadline=deadline,
    ):
        return blocked_result("source_changed_after_archive")
    if path_is_active(source, after_archive_processes, deep=True):
        return blocked_result("active_reference_after_archive")
    authority_issue = _site_removal_revalidation(
        row,
        source,
        context=after_archive_site_context,
        deadline=deadline,
    )
    if authority_issue:
        return blocked_result(authority_issue)

    _active_test_boundary_hook("after_archive_before_r2", source)
    active_test_evidence = active_test_reference_gate(source)
    if active_test_evidence.get("protected") is True:
        return blocked_result(
            "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            status="ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            active_test_reference=active_test_evidence,
        )

    try:
        if run_context is not None:
            r2 = run_r2_offload_bounded(
                archive,
                archive_sha,
                run_context=run_context,
                stage="candidate_r2",
            )
        else:
            r2 = r2_offload.offload(archive, archive_sha, delete_local=False)
    except ActiveTestFixtureReferenceProtected as exc:
        return blocked_result(
            "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            status="ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            active_test_reference=exc.evidence,
        )
    except (DeadlineExceeded, ArchivalAborted):
        cleanup_local_archive(archive)
        raise
    except Exception as exc:  # defense in depth for direct primitive callers
        r2 = {
            "r2_status": "FAILED",
            "verified": False,
            "deleted_local": False,
            "error": f"{type(exc).__name__}:{exc}"[:500],
        }
    base_result["r2_call_attempted"] = True
    if not _offhost_verified(
        r2,
        archive,
        archive_sha,
        deadline=deadline,
        run_context=run_context,
    ):
        return blocked_result("r2_upload_or_checksum_verification_failed", r2=r2)

    try:
        if run_context is not None:
            before_removal_checkpoint = run_context.checkpoint(
                "before_removal",
                require_site_context=bool(row.get("requires_site_authority")),
            )
            before_removal_processes = before_removal_checkpoint.processes
            before_removal_site_context = before_removal_checkpoint.site_context
        else:
            before_removal_processes = process_inventory()
            before_removal_site_context = None
        _check_deadline(deadline, "candidate:before_removal_revalidation")
    except BaseException:
        cleanup_local_archive(archive)
        raise
    _active_test_boundary_hook("after_r2_before_removal", source)
    active_test_evidence = active_test_reference_gate(source)
    if active_test_evidence.get("protected") is True:
        result = blocked_result(
            "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            r2=r2,
            status="ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            active_test_reference=active_test_evidence,
        )
        result["offhost_verified_before_source_removal"] = True
        return result
    if not _source_matches(
        source,
        expected_identity,
        before,
        deadline=deadline,
    ):
        result = blocked_result("source_changed_during_r2_upload", r2=r2)
        result["offhost_verified_before_source_removal"] = True
        return result
    if path_is_active(source, before_removal_processes, deep=True):
        result = blocked_result("active_reference_after_r2_verification", r2=r2)
        result["offhost_verified_before_source_removal"] = True
        return result
    authority_issue = _site_removal_revalidation(
        row,
        source,
        context=before_removal_site_context,
        deadline=deadline,
    )
    if authority_issue:
        result = blocked_result(authority_issue, r2=r2)
        result["offhost_verified_before_source_removal"] = True
        return result
    if run_context is not None:
        try:
            run_context.destructive_reference_boundary(
                source,
                deadline=deadline,
                exclude_pids={os.getpid()},
            )
        except BaseException as exc:
            result = blocked_result(
                f"destructive_reference_or_authority_drift:{type(exc).__name__}:{exc}",
                r2=r2,
            )
            result["offhost_verified_before_source_removal"] = True
            return result

    try:
        assert_free_floor("before_removal")
        _check_deadline(deadline, "candidate:removal_boundary")
        if expected_identity["type"] == "regular_file":
            free_before = _filesystem_free_bytes(source.parent)
            local_ledger = (
                run_context.effect_ledger if run_context is not None else EffectLedger()
            )

            def record_regular_effect(frame: dict[str, Any]) -> None:
                _record_effect_frame(
                    frame,
                    source_scope=str(source),
                    run_context=run_context,
                    local_ledger=local_ledger,
                )

            post_unlink_failure: PostUnlinkDurabilityError | None = None
            try:
                regular_outcome = _remove_regular_unchanged(
                    source,
                    expected_identity,
                    expected_sha=before.get("single_file_sha256"),
                    deadline=deadline,
                    effect_recorder=record_regular_effect,
                    source_scope=str(source),
                    object_removed=True,
                )
            except PostUnlinkDurabilityError as exc:
                regular_outcome = dict(exc.outcome)
                post_unlink_failure = exc
                if run_context is not None and regular_outcome.get("effects_uncertain"):
                    run_context.mark_effects_uncertain(
                        str(source), f"regular_post_unlink:{exc.stage}:{exc.cause}"
                    )
            removals = local_ledger.removals_for(str(source))
            measurement_error: BaseException | None = None
            try:
                _check_deadline(deadline, "candidate:regular_post_unlink_measurement")
                free_after = _filesystem_free_bytes(source.parent)
                reclaimed_bytes = max(0, free_after - free_before)
            except BaseException as exc:
                measurement_error = exc
                reclaimed_bytes = 0
            durability_error = post_unlink_failure or measurement_error
            local_ledger.record_status(
                source_path=str(source),
                status=(
                    "durability_confirmed"
                    if durability_error is None
                    else "post_unlink_failure"
                ),
                detail=(
                    None
                    if durability_error is None
                    else f"{type(durability_error).__name__}:{durability_error}"[:1000]
                ),
            )
            removal = {
                "removal_complete": durability_error is None,
                "source_removed": True,
                "source_retained": False,
                "source_partially_removed": False,
                "source_state_uncertain": False,
                "effects_uncertain": bool(regular_outcome.get("effects_uncertain", False)),
                "removed_entry_count": len(removals),
                "removed_file_count": sum(
                    event.get("kind") == "regular_file" for event in removals
                ),
                "removed_directory_count": 0,
                "removed_symlink_count": 0,
                "removed_logical_bytes": sum(
                    int(event.get("logical_bytes", 0)) for event in removals
                ),
                "removed_allocated_bytes_estimate": sum(
                    int(event.get("allocated_bytes_delta", 0)) for event in removals
                ),
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_bytes_uncertain": measurement_error is not None,
                "worker_error": (
                    None
                    if durability_error is None
                    else f"post_unlink_durability_failure:{type(durability_error).__name__}:"
                    f"{durability_error}"[:1000]
                ),
                "worker_deadline_exhausted": isinstance(
                    durability_error, DeadlineExceeded
                ),
                "durability_confirmed": durability_error is None,
                "effect_ledger_event_count": len(removals),
            }
        else:
            if _privileged_candidate_removal_required(source, before):
                retrieval = _retrieve_r2_archive_bounded(
                    r2,
                    archive_sha,
                    archive.lstat().st_size,
                    run_context=run_context,
                    deadline=deadline,
                )
                r2["retrieval_before_privileged_removal"] = retrieval
                if retrieval.get("retrievable") is not True:
                    result = blocked_result(
                        "r2_archive_retrieval_verification_failed", r2=r2
                    )
                    result["offhost_verified_before_source_removal"] = True
                    return result
                removal = _remove_privileged_candidate(
                    source,
                    archive,
                    archive_sha,
                    before,
                    content,
                    retrieval,
                    run_context=run_context,
                    deadline=deadline,
                )
            else:
                removal = _remove_source_unchanged(
                    source,
                    expected_identity,
                    before,
                    content,
                    deadline=deadline,
                    run_context=run_context,
                )
    except ActiveTestFixtureReferenceProtected as exc:
        result = blocked_result(
            "ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            r2=r2,
            status="ACTIVE_TEST_FIXTURE_REFERENCE_PROTECTED",
            active_test_reference=exc.evidence,
        )
        result["offhost_verified_before_source_removal"] = True
        return result
    except (DeadlineExceeded, ArchivalAborted):
        cleanup_local_archive(archive)
        raise
    except SourceDriftError:
        result = blocked_result("source_changed_immediately_before_removal", r2=r2)
        result["offhost_verified_before_source_removal"] = True
        return result
    except Exception as exc:
        reason = f"source_removal_failed:{type(exc).__name__}:{exc}"
        scoped_events = (
            run_context.effect_ledger.events_for(str(source))
            if run_context is not None
            else []
        )
        if run_context is not None and any(
            event.get("event") in {"removal_started", "removal_effect"}
            for event in scoped_events
        ):
            ledger_failure = _candidate_failure_from_effect_ledger(
                row,
                run_context=run_context,
                status="BLOCKED",
                reason=reason,
            )
            removals = run_context.effect_ledger.removals_for(str(source))
            expected_rows = before.get("manifest_rows") or []
            expected_entries = {
                (str(manifest_row[0]), str(manifest_row[1]))
                for manifest_row in expected_rows
            }
            completed_entries = {
                (str(event.get("relative")), str(event.get("kind")))
                for event in removals
            }
            ledger_counts_exact = bool(
                ledger_failure.get("source_removed") is True
                and expected_entries
                and len(removals) == len(expected_entries)
                and completed_entries == expected_entries
                and str(source) not in run_context.effect_ledger.uncertain_scopes
            )
            ledger_failure["effect_ledger_counts_exact"] = ledger_counts_exact
            ledger_failure["effect_ledger_lower_bound"] = not ledger_counts_exact
            return {
                **base_result,
                **ledger_failure,
                "r2": r2,
                "offhost_verified_before_source_removal": True,
                "local_archive_cleanup": {
                    "path": str(archive),
                    "attempted": False,
                    "deleted": False,
                    "status": "RETAINED_FOR_RECOVERY",
                },
            }
        result = blocked_result(
            reason,
            r2=r2,
            cleanup_if_source_retained=False,
        )
        result["offhost_verified_before_source_removal"] = True
        return result

    if removal.get("worker_error") is not None or not removal["removal_complete"]:
        release_reference_error = _privileged_release_reference_error(removal)
        partial = bool(removal["source_partially_removed"])
        cleanup = (
            {
                "path": str(archive),
                "attempted": False,
                "deleted": False,
                "status": "RETAINED_FOR_PARTIAL_RECOVERY",
            }
            if removal["removed_entry_count"]
            else cleanup_local_archive(archive)
        )
        return {
            **base_result,
            "status": (
                "RELEASE_REFERENCE_PROTECTED"
                if release_reference_error is not None and cleanup.get("deleted") is True
                else "BLOCKED_REMOVAL_DURABILITY"
                if "durability" in str(removal.get("worker_error") or "")
                else (
                    "BLOCKED_PARTIAL"
                    if partial
                    else (
                        "BLOCKED_REMOVAL_TERMINAL_UNCERTAIN"
                        if removal["source_removed"]
                        else "BLOCKED"
                    )
                )
            ),
            "reason": str(
                release_reference_error
                or removal.get("worker_error")
                or "directory_removal_reconciliation_incomplete"
            ),
            "r2": r2,
            "source_retained": bool(removal["source_retained"]),
            "source_removed": bool(removal["source_removed"]),
            "source_partially_removed": partial,
            "source_state_uncertain": bool(removal["source_state_uncertain"]),
            "effects_uncertain": bool(removal.get("effects_uncertain", False)),
            "removed_entry_count": int(removal["removed_entry_count"]),
            "removed_file_count": int(removal["removed_file_count"]),
            "removed_directory_count": int(removal["removed_directory_count"]),
            "removed_symlink_count": int(removal["removed_symlink_count"]),
            "removed_logical_bytes": int(removal["removed_logical_bytes"]),
            "removed_allocated_bytes_estimate": int(
                removal["removed_allocated_bytes_estimate"]
            ),
            "reclaimed_bytes": int(removal["reclaimed_bytes"]),
            "removal_reconciliation": removal,
            "offhost_verified_before_source_removal": True,
            "local_archive_cleanup": cleanup,
        }

    cleanup = cleanup_local_archive(archive)
    status = "ARCHIVED_REMOVED" if cleanup["deleted"] else "ARCHIVED_REMOVED_LOCAL_CLEANUP_FAILED"
    return {
        **base_result,
        "status": status,
        "r2": r2,
        "source_retained": False,
        "source_removed": True,
        "source_partially_removed": False,
        "source_state_uncertain": False,
        "effects_uncertain": False,
        "removed_entry_count": int(removal["removed_entry_count"]),
        "removed_file_count": int(removal["removed_file_count"]),
        "removed_directory_count": int(removal["removed_directory_count"]),
        "removed_symlink_count": int(removal["removed_symlink_count"]),
        "removed_logical_bytes": int(removal["removed_logical_bytes"]),
        "removed_allocated_bytes_estimate": int(
            removal["removed_allocated_bytes_estimate"]
        ),
        "reclaimed_bytes": int(removal["reclaimed_bytes"]),
        "removal_reconciliation": removal,
        "offhost_verified_before_source_removal": True,
        "local_archive_cleanup": cleanup,
    }


def _revalidate_batch(
    row: dict[str, Any], *, deadline: RunDeadline | None = None
) -> None:
    _check_deadline(deadline, "cron_batch:revalidate_start")
    job_root = Path(row["path"])
    try:
        root_stat = job_root.lstat()
    except OSError as exc:
        raise SourceDriftError("cron_job_root_unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise SourceDriftError("cron_job_root_invalid")
    retained_expected = row.get("retained_newest")
    if not isinstance(retained_expected, dict):
        raise SourceDriftError("cron_retained_newest_manifest_missing")
    retained_current = _snapshot_batch_member(
        Path(retained_expected["path"]), job_root, deadline=deadline
    )
    for key, value in retained_expected.items():
        if retained_current.get(key) != value:
            raise SourceDriftError(f"cron_retained_newest_drift:{key}")
    current: list[dict[str, Any]] = []
    for expected in row["members"]:
        _check_deadline(deadline, "cron_batch:revalidate_members")
        path = Path(expected["path"])
        snapshot = _snapshot_batch_member(path, job_root, deadline=deadline)
        current.append(snapshot)
        for key, value in expected.items():
            if snapshot.get(key) != value:
                raise SourceDriftError(f"cron_member_drift:{path}:{key}")
    if _batch_manifest_sha(current) != row["stats"]["manifest_sha256"]:
        raise SourceDriftError("cron_batch_manifest_drift")


def _wait_cron_member_approval(
    control_fd: int,
    *,
    deadline: RunDeadline,
    member_index: int,
) -> None:
    while True:
        deadline.check(f"cron_member:{member_index}:approval_wait")
        readable, _, _ = select.select(
            [control_fd], [], [], min(WORKER_POLL_SECONDS, deadline.remaining())
        )
        if not readable:
            continue
        command = os.read(control_fd, 1)
        if command == b"U":
            return
        if not command:
            raise ArchivalAborted(f"cron_member_approval_closed:{member_index}")
        raise ArchivalAborted(
            f"cron_member_approval_invalid:{member_index}:{command!r}"
        )


def _remove_cron_members_worker(
    row: dict[str, Any],
    write_fd: int,
    control_fd: int,
    deadline: RunDeadline,
) -> dict[str, Any]:
    _revalidate_batch(row, deadline=deadline)
    completed = 0
    for member_index, member in enumerate(row["members"]):
        path = Path(member["path"])
        expected_identity = {
            key: member[key]
            for key in (
                "device",
                "inode",
                "type",
                "size",
                "mtime_ns",
                "mode",
                "uid",
                "gid",
                "nlink",
                "blocks",
            )
        }

        def before_unlink(
            *,
            current_index: int = member_index,
            current_member: dict[str, Any] = member,
        ) -> None:
            _worker_emit(
                write_fd,
                {
                    "event": "removal_started",
                    "member_index": current_index,
                    "relative": str(current_member["relative_path"]),
                    "path": str(current_member["path"]),
                    "kind": "regular_file",
                    "operation": "unlink",
                },
            )
            _wait_cron_member_approval(
                control_fd,
                deadline=deadline,
                member_index=current_index,
            )

        def emit_completed(
            frame: dict[str, Any],
            *,
            current_index: int = member_index,
            current_member: dict[str, Any] = member,
        ) -> None:
            value = dict(frame)
            value.update(
                {
                    "member_index": current_index,
                    "relative": str(current_member["relative_path"]),
                    "path": str(current_member["path"]),
                    "object_removed": True,
                    "provenance": "cron_worker_completed_frame",
                }
            )
            _worker_emit(write_fd, value)

        _remove_regular_unchanged(
            path,
            expected_identity,
            expected_sha=str(member["sha256"]),
            deadline=deadline,
            before_unlink=before_unlink,
            effect_recorder=emit_completed,
            source_scope=str(row["path"]),
            object_removed=True,
        )
        completed += 1
        _worker_emit(
            write_fd,
            {
                "event": "removal_durable",
                "member_index": member_index,
                "relative": str(member["relative_path"]),
                "path": str(path),
            },
        )
    return {"complete": True, "completed_member_count": completed}


def _verify_batch_tar(
    archive: Path,
    members: list[dict[str, Any]],
    *,
    deadline: RunDeadline | None = None,
) -> int:
    """Stream-verify an exact cron manifest without materializing getmembers()."""

    _check_deadline(deadline, "cron_batch:verify_tar_start")
    verified = 0
    with tarfile.open(archive, "r:gz") as tf:
        for expected in members:
            _check_deadline(deadline, "cron_batch:verify_tar_members")
            _worker_test_hook("cron_verify_before_member", verified)
            info = tf.next()
            if info is None:
                raise RuntimeError("cron_archive_exact_member_manifest_truncated")
            if info.name != expected["archive_path"]:
                raise RuntimeError("cron_archive_exact_member_manifest_mismatch")
            if not info.isfile() or info.size != expected["size"]:
                raise RuntimeError(f"cron_archive_member_size_or_type_mismatch:{info.name}")
            extracted = tf.extractfile(info)
            if extracted is None:
                raise RuntimeError(f"cron_archive_member_unreadable:{info.name}")
            digest = hashlib.sha256()
            while True:
                _check_deadline(deadline, "cron_batch:verify_tar_content")
                _worker_test_hook("cron_verify_content", info.name)
                chunk = extracted.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != expected["sha256"]:
                raise RuntimeError(f"cron_archive_member_checksum_mismatch:{info.name}")
            verified += 1
        _worker_test_hook("cron_verify_before_extra_member_check", verified)
        _check_deadline(deadline, "cron_batch:verify_tar_extra_member")
        if tf.next() is not None:
            raise RuntimeError("cron_archive_exact_member_manifest_extra_member")
    return verified


def _cron_member_identity(member: dict[str, Any], value: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and value.st_dev == member["device"]
        and value.st_ino == member["inode"]
        and value.st_size == member["size"]
        and value.st_mtime_ns == member["mtime_ns"]
        and value.st_mode == member["mode"]
        and value.st_nlink == member["nlink"]
    )


def _add_bound_cron_member(
    tf: tarfile.TarFile,
    member: dict[str, Any],
    *,
    deadline: RunDeadline,
) -> None:
    """Add one descriptor-bound regular file; never follow a replacement link."""

    path = Path(member["path"])
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _cron_member_identity(member, opened) or not _cron_member_identity(member, entry):
            raise SourceDriftError(f"cron_member_identity_drift_before_archive:{path}")
        deadline.check("cron_batch:writer_before_addfile")
        info = tarfile.TarInfo(member["archive_path"])
        info.type = tarfile.REGTYPE
        info.size = opened.st_size
        info.mode = stat.S_IMODE(opened.st_mode)
        info.mtime = opened.st_mtime
        info.uid = opened.st_uid
        info.gid = opened.st_gid
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source_handle:
            class _DeadlineBoundReader:
                def read(self, size: int = -1) -> bytes:
                    deadline.check("cron_batch:writer_member_read")
                    payload = source_handle.read(size)
                    deadline.check("cron_batch:writer_member_read_complete")
                    return payload

            tf.addfile(info, _DeadlineBoundReader())
        if not _cron_member_identity(member, os.fstat(descriptor)):
            raise SourceDriftError(f"cron_member_identity_drift_during_archive:{path}")
        deadline.check("cron_batch:writer_after_addfile")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _cron_archive_verify_worker(
    partial: Path,
    members: list[dict[str, Any]],
    write_fd: int,
    deadline: RunDeadline,
) -> dict[str, Any]:
    deadline.check("cron_batch:writer_open")
    with tarfile.open(partial, "w:gz") as tf:
        tf.dereference = True
        for member_index, member in enumerate(members):
            deadline.check("cron_batch:writer_members")
            _worker_test_hook("cron_writer_before_member", member_index)
            _add_bound_cron_member(tf, member, deadline=deadline)
            _worker_emit(
                write_fd,
                {
                    "event": "archive_member_written",
                    "archive_path": member["archive_path"],
                    "member_index": member_index,
                },
            )
        deadline.check("cron_batch:writer_before_gzip_close")
        _worker_test_hook("cron_writer_before_gzip_close", len(members))
    deadline.check("cron_batch:writer_after_gzip_close")
    _worker_test_hook("cron_writer_after_gzip_close", len(members))
    os.chmod(partial, 0o600)
    verified = _verify_batch_tar(partial, members, deadline=deadline)
    _worker_test_hook("cron_verify_before_digest", str(partial))
    archive_sha = sha256_file(partial, deadline=deadline)
    archive_stat = partial.lstat()
    if stat.S_ISLNK(archive_stat.st_mode) or not stat.S_ISREG(archive_stat.st_mode):
        raise RuntimeError("cron_archive_regular_nonsymlink_required")
    deadline.check("cron_batch:archive_worker_complete")
    return {
        "verified": True,
        "archive_sha256": archive_sha,
        "archive_size_bytes": archive_stat.st_size,
        "member_count": verified,
        "exact_manifest_verified": True,
    }


def archive_file_batch(
    row: dict[str, Any],
    archive_dir: Path,
    processes: ProcessReferenceSnapshot | list[dict[str, Any]],
    *,
    run_context: RunContext | None = None,
) -> dict[str, Any]:
    assert row.get("kind") == "cron_output_batch"
    deadline = run_context.deadline if run_context is not None else None
    if run_context is not None:
        run_context.assert_owned()
        before_checkpoint = run_context.checkpoint("before_archive")
        processes = before_checkpoint.processes
    _check_deadline(deadline, "cron_batch:before_archive")
    job_root = Path(row["path"])
    archive_dir.mkdir(parents=True, exist_ok=True)
    assert_free_floor("before_cron_batch_archive")
    _revalidate_batch(row, deadline=deadline)
    if path_is_active(job_root, processes, deep=True):
        return {
            "path": str(job_root),
            "kind": "cron_output_batch",
            "status": "BLOCKED",
            "reason": "cron_job_active_before_archive",
            "removed_file_count": 0,
            "source_removed": False,
            "reclaimed_bytes": 0,
        }
    archive = archive_dir / f"cron-output__{row['job_id']}-{stamp()}.tar.gz"
    partial = archive.with_suffix(archive.suffix + ".partial")
    operation_deadline = deadline or RunDeadline.start(TAR_MAX_SECONDS)
    try:
        worker_result, _worker_events = _with_free_space_guard(
            lambda: run_loaded_worker_bounded(
                lambda write_fd, worker_deadline: _cron_archive_verify_worker(
                    partial,
                    row["members"],
                    write_fd,
                    worker_deadline,
                ),
                run_context=run_context,
                stage="cron_archive_verify",
                cap_seconds=TAR_MAX_SECONDS,
                operation_deadline=operation_deadline,
            ),
            run_context=run_context,
        )
        partial_stat = partial.lstat()
        if (
            stat.S_ISLNK(partial_stat.st_mode)
            or not stat.S_ISREG(partial_stat.st_mode)
            or partial_stat.st_size != worker_result.get("archive_size_bytes")
            or worker_result.get("verified") is not True
            or worker_result.get("exact_manifest_verified") is not True
            or worker_result.get("member_count") != len(row["members"])
        ):
            raise RuntimeError("cron_archive_worker_result_or_identity_mismatch")
        archive_sha = str(worker_result["archive_sha256"])
        if not SHA256_RE.fullmatch(archive_sha):
            raise RuntimeError("cron_archive_worker_digest_invalid")
        _check_deadline(deadline, "cron_batch:before_archive_commit")
        if run_context is not None:
            run_context.assert_worker_boundary(deadline=deadline)
        os.replace(partial, archive)
        if run_context is not None:
            run_context.effects["archive_files_created"] += 1
    except BaseException as exc:
        _cleanup_partial_or_fail(partial, exc)
        raise

    base = {
        "path": str(job_root),
        "kind": "cron_output_batch",
        "job_id": row["job_id"],
        "reason": row["reason"],
        "archive": str(archive),
        "archive_sha256": archive_sha,
        "archive_created": True,
        "r2_call_attempted": False,
        "batch_manifest_sha256": row["stats"]["manifest_sha256"],
        "source_files": len(row["members"]),
        "source_logical_bytes": row["stats"]["logical_bytes"],
        "source_allocated_bytes": row["stats"]["allocated_bytes"],
        "source_externally_linked_allocated_bytes": row["stats"][
            "externally_linked_allocated_bytes"
        ],
        "source_reclaimable_allocated_bytes": row["stats"]["reclaimable_allocated_bytes"],
        "retained_newest_path": row["retained_newest_path"],
    }

    def blocked(reason: str, *, r2: dict[str, Any] | None = None) -> dict[str, Any]:
        offhost_verified = bool(
            r2
            and _offhost_verified(
                r2,
                archive,
                archive_sha,
                deadline=deadline,
                run_context=run_context,
            )
        )
        cleanup = cleanup_local_archive(archive)
        result = {
            **base,
            "status": "BLOCKED",
            "reason": reason,
            "removed_file_count": 0,
            "source_removed": False,
            "source_retained": True,
            "reclaimed_bytes": 0,
            "offhost_verified_before_source_removal": offhost_verified,
            "local_archive_cleanup": cleanup,
        }
        if r2 is not None:
            result["r2"] = r2
        return result

    try:
        if run_context is not None:
            after_archive_checkpoint = run_context.checkpoint("before_r2")
            after_archive_processes = after_archive_checkpoint.processes
        else:
            after_archive_processes = process_inventory()
        _revalidate_batch(row, deadline=deadline)
    except SourceDriftError as exc:
        return blocked(f"cron_batch_changed_after_archive:{exc}")
    except (DeadlineExceeded, ArchivalAborted):
        cleanup_local_archive(archive)
        raise
    assert_free_floor("after_cron_batch_archive_before_offload")
    if path_is_active(job_root, after_archive_processes, deep=True):
        return blocked("cron_job_active_after_archive", r2=None)
    try:
        if run_context is not None:
            r2 = run_r2_offload_bounded(
                archive,
                archive_sha,
                run_context=run_context,
                stage="cron_batch_r2",
            )
        else:
            r2 = r2_offload.offload(archive, archive_sha, delete_local=False)
    except (DeadlineExceeded, ArchivalAborted):
        cleanup_local_archive(archive)
        raise
    except Exception as exc:
        r2 = {
            "r2_status": "FAILED",
            "verified": False,
            "deleted_local": False,
            "error": f"{type(exc).__name__}:{exc}"[:500],
        }
    base["r2_call_attempted"] = True
    if not _offhost_verified(
        r2,
        archive,
        archive_sha,
        deadline=deadline,
        run_context=run_context,
    ):
        return blocked("r2_upload_or_checksum_verification_failed", r2=r2)
    try:
        if run_context is not None:
            before_removal_checkpoint = run_context.checkpoint("before_removal")
            latest_processes = before_removal_checkpoint.processes
        else:
            latest_processes = process_inventory()
        _revalidate_batch(row, deadline=deadline)
    except SourceDriftError as exc:
        return blocked(f"cron_batch_changed_during_r2_upload:{exc}", r2=r2)
    except (DeadlineExceeded, ArchivalAborted):
        cleanup_local_archive(archive)
        raise
    if path_is_active(job_root, latest_processes, deep=True):
        return blocked("cron_job_active_after_r2_verification", r2=r2)
    if run_context is not None:
        try:
            run_context.destructive_reference_boundary(
                job_root,
                deadline=deadline,
                exclude_pids={os.getpid()},
            )
        except BaseException as exc:
            return blocked(
                f"cron_destructive_reference_or_authority_drift:{type(exc).__name__}:{exc}",
                r2=r2,
            )

    # Each worker starts a descriptor-bound member, then blocks on a parent
    # approval gate.  The parent refreshes live process/fd/reference state and
    # only then releases that exact unlink.
    free_before = _filesystem_free_bytes(job_root)
    local_ledger = run_context.effect_ledger if run_context is not None else EffectLedger()
    events: list[dict[str, Any]] = []
    removal_error: BaseException | None = None
    removal_worker_result: dict[str, Any] | None = None
    local_reference_refreshes = 0
    approved_member_indexes: set[int] = set()
    expected_by_path = {str(member["path"]): member for member in row["members"]}

    def record_cron_effect(frame: dict[str, Any]) -> None:
        _record_effect_frame(
            frame,
            source_scope=str(job_root),
            run_context=run_context,
            local_ledger=local_ledger,
        )

    def handle_cron_worker_event(
        frame: dict[str, Any],
        worker_pid: int,
        monitor_deadline: RunDeadline,
    ) -> bytes | None:
        nonlocal local_reference_refreshes
        event = frame.get("event")
        if event == "removal_completed":
            record_cron_effect(frame)
            return None
        if event != "removal_started":
            return None
        _record_started_frame(
            frame,
            source_scope=str(job_root),
            ledger=local_ledger,
        )
        member_index = int(frame.get("member_index", -1))
        if member_index < 0 or member_index >= len(row["members"]):
            raise SourceDriftError("cron_worker_started_index_invalid")
        if member_index in approved_member_indexes:
            raise SourceDriftError(
                f"cron_worker_started_index_duplicate:{member_index}"
            )
        expected = row["members"][member_index]
        member_path = Path(expected["path"])
        if str(frame.get("path")) != str(member_path):
            raise SourceDriftError(
                f"cron_worker_started_path_mismatch:{member_index}"
            )
        local_reference_refreshes += 1
        if run_context is not None:
            run_context.cron_member_reference_boundary(
                member_path,
                deadline=monitor_deadline,
                exclude_pids={os.getpid(), worker_pid},
            )
        else:
            snapshot = _call_process_inventory(
                deadline=monitor_deadline,
                exclude_pids={os.getpid(), worker_pid},
            )
            if path_is_active(member_path, snapshot, deep=True):
                raise SourceDriftError(
                    f"cron_member_became_referenced:{member_path}"
                )
        approved_member_indexes.add(member_index)
        return b"U"

    try:
        assert_free_floor("before_cron_batch_removal")
        removal_operation_deadline = deadline or RunDeadline.start(REMOVAL_MAX_SECONDS)
        removal_worker_result, events = _with_free_space_guard(
            lambda: run_loaded_worker_bounded(
                lambda write_fd, control_fd, worker_deadline: _remove_cron_members_worker(
                    row,
                    write_fd,
                    control_fd,
                    worker_deadline,
                ),
                run_context=run_context,
                stage="descriptor_safe_cron_removal",
                cap_seconds=REMOVAL_MAX_SECONDS,
                operation_deadline=removal_operation_deadline,
                event_handler=handle_cron_worker_event,
                worker_control=True,
            ),
            run_context=run_context,
        )
    except WorkerSupervisionError as exc:
        removal_error = exc
        events = list(exc.events)
    except BaseException as exc:
        removal_error = exc

    # Streaming delivery normally committed these already.  Repeat
    # idempotently before any expensive state reconciliation.
    for frame in events:
        if frame.get("event") == "removal_completed":
            record_cron_effect(frame)

    cleanup_deadline = _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=(deadline or RunDeadline.start(REMOVAL_MAX_SECONDS)),
        stage="cron_batch_removal_reconcile",
    )
    started_paths = {
        str(frame.get("path"))
        for frame in events
        if frame.get("event") == "removal_started"
    }
    completed_frame_paths = {
        str(frame.get("path"))
        for frame in events
        if frame.get("event") == "removal_completed"
    }
    drifted_paths: list[str] = []
    physically_removed: set[str] = set()
    unresolved_started = set(started_paths - completed_frame_paths)
    reconciliation_error: BaseException | None = None

    def record_reconciled_member(member: dict[str, Any], provenance: str) -> None:
        record_cron_effect(
            {
                "event": "removal_completed",
                "path": str(member["path"]),
                "relative": str(member["relative_path"]),
                "operation": "unlink",
                "kind": "regular_file",
                "size": int(member["size"]),
                "blocks": int(member.get("blocks", int(member["allocated_bytes"]) // 512)),
                "device": int(member["device"]),
                "inode": int(member["inode"]),
                "nlink": int(member["nlink"]),
                "object_removed": True,
                "provenance": provenance,
            }
        )

    try:
        # Direct bounded checks for started-without-completed events happen
        # first, before the whole selected-member pass.
        for path_text in sorted(unresolved_started):
            cleanup_deadline.check("cron_batch:reconcile_started_effect")
            member = expected_by_path.get(path_text)
            if member is None:
                raise SourceDriftError(
                    f"cron_started_event_unknown_path:{path_text}"
                )
            path = Path(path_text)
            try:
                current = path.lstat()
            except FileNotFoundError:
                physically_removed.add(path_text)
                record_reconciled_member(
                    member, "bounded_started_effect_reconciliation"
                )
                unresolved_started.discard(path_text)
                continue
            except OSError:
                drifted_paths.append(path_text)
                continue
            if _cron_member_identity(member, current):
                unresolved_started.discard(path_text)
            else:
                drifted_paths.append(path_text)

        for member in row["members"]:
            cleanup_deadline.check("cron_batch:removal_reconcile")
            path = Path(member["path"])
            path_text = str(path)
            try:
                current = path.lstat()
            except FileNotFoundError:
                physically_removed.add(path_text)
                record_reconciled_member(member, "full_manifest_reconciliation")
                continue
            except OSError:
                drifted_paths.append(path_text)
                continue
            if not _cron_member_identity(member, current):
                drifted_paths.append(path_text)
    except BaseException as exc:
        reconciliation_error = exc
        if run_context is not None:
            run_context.mark_effects_uncertain(
                str(job_root),
                f"cron_reconciliation:{type(exc).__name__}:{exc}",
            )
        else:
            local_ledger.mark_uncertain(
                str(job_root),
                f"cron_reconciliation:{type(exc).__name__}:{exc}",
            )
        removal_error = removal_error or exc

    ledger_removals = local_ledger.removals_for(str(job_root))
    ledger_removed_paths = {str(event["target_path"]) for event in ledger_removals}
    physically_removed.update(ledger_removed_paths)
    removed = len(ledger_removals)
    newly_reconciled = ledger_removed_paths - completed_frame_paths
    effects_uncertain = bool(
        reconciliation_error is not None
        or drifted_paths
        or unresolved_started
        or str(job_root) in local_ledger.uncertain_scopes
    )
    if effects_uncertain:
        if run_context is not None:
            run_context.mark_effects_uncertain(
                str(job_root), "cron_effects_or_state_uncertain"
            )
        else:
            local_ledger.mark_uncertain(
                str(job_root), "cron_effects_or_state_uncertain"
            )
    if run_context is not None:
        try:
            run_context.assert_worker_boundary(deadline=cleanup_deadline)
        except BaseException as exc:
            removal_error = removal_error or exc
    if (
        removal_worker_result is not None
        and removal_worker_result.get("complete") is not True
    ):
        removal_error = removal_error or RuntimeError(
            "cron_removal_worker_nonterminal_result"
        )

    removed_allocated_estimate = sum(
        int(event.get("allocated_bytes_delta", 0)) for event in ledger_removals
    )
    removed_logical = sum(
        int(event.get("logical_bytes", 0)) for event in ledger_removals
    )
    measurement_error: BaseException | None = None
    try:
        cleanup_deadline.check("cron_batch:before_free_space_sample")
        free_after = _filesystem_free_bytes(job_root)
        reclaimed_bytes = max(0, free_after - free_before)
    except BaseException as exc:
        measurement_error = exc
        reclaimed_bytes = 0
        removal_error = removal_error or exc
    complete = (
        removed == len(row["members"])
        and not drifted_paths
        and not unresolved_started
        and not effects_uncertain
    )
    possible_effect = bool(ledger_removals or started_paths)
    partial = 0 < removed < len(row["members"])
    source_retained = bool(not possible_effect and not drifted_paths)
    durability_failure = bool(
        removal_error is not None
        and (
            "PostUnlinkDurabilityError" in f"{type(removal_error).__name__}:{removal_error}"
            or "parent_fsync" in str(removal_error)
        )
    )
    local_ledger.record_status(
        source_path=str(job_root),
        status=(
            "durability_failed"
            if durability_failure
            else ("complete" if complete else "partial_or_blocked")
        ),
        detail=(
            None
            if removal_error is None
            else f"{type(removal_error).__name__}:{removal_error}"[:1000]
        ),
    )

    if removal_error is not None or not complete:
        cleanup = (
            cleanup_local_archive(archive)
            if not possible_effect
            else {
                "path": str(archive),
                "attempted": False,
                "deleted": False,
                "status": "RETAINED_FOR_PARTIAL_RECOVERY",
            }
        )
        status = (
            "BLOCKED_REMOVAL_DURABILITY"
            if durability_failure
            else (
                "BLOCKED_PARTIAL"
                if partial or (possible_effect and not complete)
                else "BLOCKED_REMOVAL_TERMINAL_UNCERTAIN"
                if effects_uncertain
                else "BLOCKED"
            )
        )
        return {
            **base,
            "status": status,
            "reason": (
                f"cron_batch_removal_failed:{type(removal_error).__name__}:"
                f"{removal_error}"
                if removal_error is not None
                else "cron_batch_removal_reconciliation_incomplete"
            ),
            "removed_file_count": removed,
            "removed_logical_bytes": removed_logical,
            "removed_allocated_bytes_estimate": removed_allocated_estimate,
            "source_removed": False,
            "selected_sources_removed": removed == len(row["members"]),
            "source_retained": source_retained,
            "source_partially_removed": bool(partial or (possible_effect and not complete)),
            "source_state_uncertain": bool(drifted_paths or unresolved_started),
            "effects_uncertain": effects_uncertain,
            "drifted_entry_count": len(drifted_paths),
            "unresolved_started_effect_count": len(unresolved_started),
            "started_effect_count": len(started_paths),
            "completed_effect_event_count": len(completed_frame_paths),
            "unlink_returned_count": len(completed_frame_paths),
            "unlink_reconciled_after_interrupt_count": len(newly_reconciled),
            "member_reference_refresh_count": local_reference_refreshes,
            "reclaimed_bytes": reclaimed_bytes,
            "reclaimed_bytes_uncertain": measurement_error is not None,
            "offhost_verified_before_source_removal": True,
            "local_archive_cleanup": cleanup,
            "r2": r2,
        }
    cleanup = cleanup_local_archive(archive)
    status = (
        "ARCHIVED_FILES_REMOVED"
        if cleanup["deleted"]
        else "ARCHIVED_REMOVED_LOCAL_CLEANUP_FAILED"
    )
    return {
        **base,
        "status": status,
        "removed_file_count": removed,
        "removed_logical_bytes": removed_logical,
        "removed_allocated_bytes_estimate": removed_allocated_estimate,
        "source_removed": False,
        "selected_sources_removed": True,
        "source_retained": False,
        "source_partially_removed": False,
        "source_state_uncertain": False,
        "effects_uncertain": False,
        "unlink_returned_count": len(completed_frame_paths),
        "unlink_reconciled_after_interrupt_count": len(newly_reconciled),
        "member_reference_refresh_count": local_reference_refreshes,
        "reclaimed_bytes": reclaimed_bytes,
        "reclaimed_bytes_uncertain": False,
        "offhost_verified_before_source_removal": True,
        "local_archive_cleanup": cleanup,
        "r2": r2,
    }


def root_usage() -> dict[str, int | float]:
    value = os.statvfs("/")
    total = value.f_blocks * value.f_frsize
    free = value.f_bavail * value.f_frsize
    return {
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free,
        "used_percent": round((total - free) * 100 / total, 2),
    }


def assert_free_floor(stage: str) -> None:
    usage = root_usage()
    if int(usage["free_bytes"]) < MIN_FREE_BYTES:
        raise ArchivalAborted(f"free_space_floor_breached:{stage}:{usage['free_bytes']}")


@dataclass
class FreeSpaceGuardProcess:
    pid: int
    control_fd: int
    released: bool = False

    def release(self, *, deadline: RunDeadline) -> None:
        if self.released:
            return
        _write_worker_control(
            self.control_fd,
            b"B",
            deadline=deadline,
            stage="free_space_guard_release",
        )
        self.released = True


def _close_monitor_inherited_descriptors(*, keep: set[int]) -> None:
    try:
        names = [entry.name for entry in os.scandir("/proc/self/fd")]
    except OSError:
        names = [str(value) for value in range(3, 256)]
    for name in names:
        try:
            descriptor = int(name)
        except ValueError:
            continue
        if descriptor <= 2 or descriptor in keep:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass


def start_free_space_guard(
    _legacy_stop: Any = None,
    operation_deadline: RunDeadline | None = None,
) -> FreeSpaceGuardProcess:
    """Start a gate-released monitor process; no Python guard thread exists."""

    deadline = operation_deadline or RunDeadline.start(5.0)
    deadline.check("free_space_guard:before_fork")
    parent_pid = os.getpid()
    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        for descriptor in (control_read, control_write, ready_read, ready_write):
            os.close(descriptor)
        raise
    if pid == 0:  # pragma: no cover - behavior is parent-observed
        exit_code = 0
        try:
            os.close(control_write)
            os.close(ready_read)
            os.setsid()
            _close_monitor_inherited_descriptors(keep={control_read, ready_write})
            os.write(ready_write, b"R")
            os.close(ready_write)
            released = False
            next_check = time.monotonic()
            while True:
                wait = 0.5 if released else 5.0
                if released:
                    wait = max(0.0, min(wait, next_check - time.monotonic()))
                readable, _, _ = select.select([control_read], [], [], wait)
                if readable:
                    command = os.read(control_read, 32)
                    if not command or b"S" in command:
                        break
                    if b"B" in command:
                        released = True
                        next_check = time.monotonic()
                if not released or time.monotonic() < next_check:
                    continue
                try:
                    free_bytes = int(root_usage()["free_bytes"])
                except BaseException:
                    os.kill(parent_pid, signal.SIGUSR1)
                    break
                if free_bytes < MIN_FREE_BYTES:
                    os.kill(parent_pid, signal.SIGUSR1)
                    break
                next_check = time.monotonic() + 0.5
        except BaseException:
            exit_code = 1
        finally:
            try:
                os.close(control_read)
            except OSError:
                pass
            os._exit(exit_code)

    os.close(control_read)
    os.close(ready_write)
    os.set_blocking(control_write, False)
    ready = b""
    startup_error: BaseException | None = None
    try:
        readable, _, _ = select.select(
            [ready_read], [], [], max(0.0, min(5.0, deadline.remaining()))
        )
        ready = os.read(ready_read, 1) if readable else b""
    except BaseException as exc:
        startup_error = exc
    finally:
        os.close(ready_read)
    if startup_error is None and ready != b"R":
        startup_error = RuntimeError("free_space_guard_start_failed")
    elif startup_error is None and deadline.remaining() <= 0:
        startup_error = DeadlineExceeded(
            "internal_deadline_exhausted:free_space_guard_start"
        )
    if startup_error is not None:
        cleanup_deadline = _cleanup_deadline_for(
            run_context=None,
            operation_deadline=deadline,
            stage="free_space_guard_start_cleanup",
        )
        try:
            _terminate_fork_group(
                pid,
                run_context=None,
                cleanup_deadline=cleanup_deadline,
            )
        finally:
            os.close(control_write)
        raise startup_error
    return FreeSpaceGuardProcess(pid=pid, control_fd=control_write)


def _stop_free_space_guard(
    _legacy_stop: Any,
    guard: Any,
    *,
    run_context: RunContext | None,
    cleanup_deadline: RunDeadline | None = None,
) -> None:
    deadline = cleanup_deadline or _cleanup_deadline_for(
        run_context=run_context,
        operation_deadline=(run_context.deadline if run_context is not None else None),
        stage="free_space_guard_stop",
    )
    if run_context is not None:
        run_context.free_space_guards_stopped += 1

    # Compatibility for focused test doubles; production always uses the
    # killable process protocol above.
    if not isinstance(guard, FreeSpaceGuardProcess):
        legacy_set = getattr(_legacy_stop, "set", None)
        if callable(legacy_set):
            legacy_set()
        join = getattr(guard, "join", None)
        if callable(join):
            join(timeout=min(FREE_SPACE_GUARD_JOIN_SECONDS, deadline.remaining()))
        alive_check = getattr(guard, "is_alive", None)
        alive = bool(alive_check()) if callable(alive_check) else False
        if alive:
            if run_context is not None:
                run_context.free_space_guards_survived += 1
            raise RuntimeError("free_space_guard_survived_join")
        if run_context is not None:
            run_context.free_space_guards_joined += 1
        return

    try:
        try:
            _write_worker_control(
                guard.control_fd,
                b"S",
                deadline=deadline,
                stage="free_space_guard_stop",
            )
        except (BrokenPipeError, OSError, DeadlineExceeded):
            pass
    finally:
        try:
            os.close(guard.control_fd)
        except OSError:
            pass
    reaped = False
    join_end = min(
        deadline.deadline_monotonic,
        time.monotonic() + FREE_SPACE_GUARD_JOIN_SECONDS,
    )
    while deadline.remaining() > 0 and time.monotonic() < join_end:
        try:
            waited, _status = os.waitpid(guard.pid, os.WNOHANG)
        except ChildProcessError:
            waited = guard.pid
        if waited == guard.pid:
            reaped = True
            break
        time.sleep(min(0.01, deadline.remaining(), max(0.0, join_end - time.monotonic())))
    if not reaped or _group_exists(guard.pid):
        if run_context is not None:
            run_context.free_space_guards_forced_killed += 1
        try:
            _terminate_fork_group(
                guard.pid,
                run_context=None,
                cleanup_deadline=deadline,
            )
            reaped = True
        except BaseException:
            if run_context is not None:
                run_context.free_space_guards_survived += 1
            raise
    if not reaped or _group_exists(guard.pid):
        if run_context is not None:
            run_context.free_space_guards_survived += 1
        raise RuntimeError("free_space_guard_process_survived_cleanup")
    if run_context is not None:
        run_context.free_space_guards_joined += 1


def _with_free_space_guard(
    operation: Callable[[], Any],
    *,
    run_context: RunContext | None,
) -> Any:
    operation_deadline = (
        run_context.deadline
        if run_context is not None
        else RunDeadline.start(OUTER_TIMEOUT_SECONDS)
    )
    # Kept only as a compatibility slot for old, non-production test doubles;
    # the production guard is exclusively a killable process.
    legacy_stop = None
    guard = start_free_space_guard(
        legacy_stop,
        operation_deadline=operation_deadline,
    )
    if run_context is not None:
        run_context.free_space_guards_started += 1
    operation_error: BaseException | None = None
    result: Any = None
    try:
        release = getattr(guard, "release", None)
        if callable(release):
            release(deadline=operation_deadline)
        result = operation()
    except BaseException as exc:
        operation_error = exc
    try:
        cleanup_deadline = _cleanup_deadline_for(
            run_context=run_context,
            operation_deadline=operation_deadline,
            stage="free_space_guard_operation_cleanup",
        )
        _stop_free_space_guard(
            legacy_stop,
            guard,
            run_context=run_context,
            cleanup_deadline=cleanup_deadline,
        )
    except BaseException as guard_error:
        if operation_error is None:
            raise
        raise RuntimeError(
            f"{type(operation_error).__name__}:{operation_error};free_space_guard_cleanup:"
            f"{type(guard_error).__name__}:{guard_error}"
        ) from operation_error
    if operation_error is not None:
        raise operation_error
    return result


def install_free_space_signal() -> None:
    def abort(_signum: int, _frame: Any) -> None:
        raise ArchivalAborted("free_space_floor_breached:during_archive")

    signal.signal(signal.SIGUSR1, abort)


class OpsAlertIntegrityError(RuntimeError):
    """The local alert carrier did not match its pinned, descriptor-read bytes."""


def _ops_alert_source_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def _bound_ops_alert_path(path: Path) -> os.stat_result:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise OpsAlertIntegrityError("ops_alert_invalid_canonical_path")
    current = Path(path.anchor)
    value: os.stat_result | None = None
    for component in path.parts[1:]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise OpsAlertIntegrityError("ops_alert_invalid") from exc
        if stat.S_ISLNK(value.st_mode):
            raise OpsAlertIntegrityError("ops_alert_invalid_symlink")
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise OpsAlertIntegrityError("ops_alert_invalid_parent")
    if value is None or not stat.S_ISREG(value.st_mode):
        raise OpsAlertIntegrityError("ops_alert_regular_file_required")
    if value.st_uid != os.geteuid():
        raise OpsAlertIntegrityError("ops_alert_owner_invalid")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise OpsAlertIntegrityError("ops_alert_group_or_world_writable")
    if value.st_nlink != 1:
        raise OpsAlertIntegrityError("ops_alert_link_count_invalid")
    if value.st_size > OPS_ALERT_MAX_SOURCE_BYTES:
        raise OpsAlertIntegrityError("ops_alert_oversized")
    return value


def _read_verified_ops_alert_source(
    path: Path = OPS_ALERT,
    expected_sha256: str = EXPECTED_OPS_ALERT_SHA256,
) -> bytes:
    """Read and retain exact carrier bytes without following or later reopening it."""

    before = _bound_ops_alert_path(path)
    identity = _ops_alert_source_identity(before)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OpsAlertIntegrityError("ops_alert_invalid") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _ops_alert_source_identity(opened) != identity
        ):
            raise OpsAlertIntegrityError("ops_alert_identity_drift")
        digest = hashlib.sha256()
        source = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            source.extend(chunk)
            if len(source) > OPS_ALERT_MAX_SOURCE_BYTES:
                raise OpsAlertIntegrityError("ops_alert_oversized")
        final = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise OpsAlertIntegrityError("ops_alert_identity_drift") from exc
        if (
            _ops_alert_source_identity(final) != identity
            or _ops_alert_source_identity(path_after) != identity
            or len(source) != before.st_size
        ):
            raise OpsAlertIntegrityError("ops_alert_identity_drift")
        if digest.hexdigest() != expected_sha256:
            raise OpsAlertIntegrityError("ops_alert_sha256_mismatch")
        return bytes(source)
    finally:
        os.close(descriptor)


def _verified_ops_alert_source_and_path() -> tuple[bytes, Path]:
    injected = globals().get("_F12_WRAPPER_VERIFIED_OPS_ALERT_SOURCE")
    if injected is not None:
        if not isinstance(injected, bytes):
            raise OpsAlertIntegrityError("ops_alert_injected_source_invalid")
        if hashlib.sha256(injected).hexdigest() != EXPECTED_OPS_ALERT_SHA256:
            raise OpsAlertIntegrityError("ops_alert_injected_sha256_mismatch")
        metadata = globals().get("_F12_WRAPPER_VERIFIED_OPS_ALERT_PATH", OPS_ALERT)
        if not isinstance(metadata, Path):
            raise OpsAlertIntegrityError("ops_alert_injected_path_invalid")
        if not metadata.is_absolute() or Path(os.path.normpath(str(metadata))) != metadata:
            raise OpsAlertIntegrityError("ops_alert_injected_path_invalid")
        return injected, metadata
    return _read_verified_ops_alert_source(OPS_ALERT, EXPECTED_OPS_ALERT_SHA256), OPS_ALERT


def _ops_alert_command(source: bytes, source_path: Path, message: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        OPS_ALERT_BOOTSTRAP,
        base64.b64encode(source).decode("ascii"),
        str(source_path),
        message,
    ]


def alert_if_needed(
    receipt: dict[str, Any], *, run_context: RunContext | None = None
) -> dict[str, Any] | None:
    usage = receipt["root_after"]
    if float(usage["used_percent"]) <= 85:
        return None
    message = (
        f"F12 disk archival warning\nroot {usage['used_percent']:.1f}% used / "
        f"{usage['free_bytes'] / GIB:.1f} GiB free after run\n"
        f"archived {receipt['removed_count']} sources / "
        f"{receipt['freed_bytes'] / GIB:.1f} GiB filesystem delta\n"
        f"receipt {receipt['receipt_path']}"
    )
    deadline = run_context.deadline if run_context is not None else RunDeadline.start(ALERT_MAX_SECONDS)
    try:
        ops_alert_source, ops_alert_path = _verified_ops_alert_source_and_path()
        proc = run_process_group(
            _ops_alert_command(ops_alert_source, ops_alert_path, message),
            deadline=deadline,
            cap_seconds=ALERT_MAX_SECONDS,
            stage="ops_alert",
            run_context=run_context,
            env={**os.environ, "DISCORD_CHANNEL_ID": OPS_ALERTS_CHANNEL_ID},
        )
    except OpsAlertIntegrityError as exc:
        return {
            "channel_id": OPS_ALERTS_CHANNEL_ID,
            "status": "BLOCKED",
            "error": f"{type(exc).__name__}:{exc}",
        }
    except (DeadlineExceeded, ArchivalAborted) as exc:
        return {
            "channel_id": OPS_ALERTS_CHANNEL_ID,
            "status": "DEFERRED",
            "error": f"{type(exc).__name__}:{exc}",
        }
    if run_context is not None:
        run_context.effects["notifications"] += 1
    return {
        "channel_id": OPS_ALERTS_CHANNEL_ID,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-1000:],
        "stderr": proc.stderr[-1000:],
    }


MATERIAL_FAILURE_STATUSES = {
    "ABORTED",
    "BLOCKED",
    "BLOCKED_PARTIAL",
    "BLOCKED_REMOVAL_DURABILITY",
    "BLOCKED_REMOVAL_TERMINAL_UNCERTAIN",
    "DEFERRED",
    "DEADLINE_EXHAUSTED",
    "FAILED",
    "ARCHIVED_REMOVED_LOCAL_CLEANUP_FAILED",
}


def exit_code_for_results(results: list[dict[str, Any]], *, dry_run: bool) -> int:
    return 2 if any(row.get("status") in MATERIAL_FAILURE_STATUSES for row in results) else 0


def _zero_effects() -> dict[str, int]:
    return {
        "archive_files_created": 0,
        "source_objects_removed": 0,
        "source_files_removed": 0,
        "source_directories_removed": 0,
        "r2_calls": 0,
        "notifications": 0,
        "scheduler_changes": 0,
        "receipt_files_created": 0,
    }


def _policy_receipt() -> dict[str, Any]:
    return {
        "reports_days": REPORT_RETENTION_DAYS,
        "reports_direct_children_only": True,
        "reports_minimum_allocated_bytes": MIN_REPORT_ALLOCATED,
        "site_lane_seconds": dict(SITE_LANE_RETENTION_SECONDS),
        "site_removal_lanes": sorted(SITE_REMOVAL_LANES),
        "release_removal_policy": "never_selected_authority_and_reference_input_only",
        "site_authority_required": "verified_current-content-base_with_hash_pinned_companions",
        "candidate_retention_proof": "exact_same_name_regular_release_directory_only",
        "active_rollback_receipts_expire_by_mtime": False,
        "active_rollback_registry": {
            "path": str(ROLLBACK_REGISTRY_PATH),
            "builds": "one_descriptor_bound_load_per_run_then_bounded_member_revalidation",
            "schema": ROLLBACK_REGISTRY_SCHEMA,
            "immutable_version": ROLLBACK_REGISTRY_IMMUTABLE_VERSION,
            "max_registry_bytes": ROLLBACK_REGISTRY_MAX_BYTES,
            "max_entries": ROLLBACK_REGISTRY_MAX_ENTRIES,
            "max_roots_per_entry": ROLLBACK_REGISTRY_MAX_ROOTS_PER_ENTRY,
            "max_entry_bytes": ROLLBACK_AUTHORITY_ENTRY_MAX_BYTES,
            "candidate_admission": "explicit_registry_membership_only",
            "filename_admission": False,
            "full_reports_corpus_walks": 0,
            "unregistered_json_content_reads": 0,
            "missing_malformed_or_drifted": "fail_closed_site_lanes",
        },
        "common_f12_f13_lock": str(LOCK_PATH),
        "process_reference_scan": "one_per_named_checkpoint_plus_fresh_destructive_boundary_and_supervised_worker_monitor",
        "candidate_overlap_algorithm": "path_depth_map_sort_O_n_log_n",
        "internal_deadline_seconds": INTERNAL_DEADLINE_MAX_SECONDS,
        "outer_timeout_seconds": OUTER_TIMEOUT_SECONDS,
        "cleanup_receipt_margin_seconds": CLEANUP_RECEIPT_MARGIN_SECONDS,
        "child_process_cleanup": "dedicated_session_terminate_kill_reap_group",
        "cron_archive_worker": "forked_loaded_code_setsid_deadline_terminate_kill_reap",
        "cron_archive_verification": "streaming_exact_names_types_sizes_content_sha256_no_getmembers",
        "directory_removal": "unprivileged_descriptor_bound_or_candidate_only_root_quarantine_helper",
        "privileged_sealed_candidate_removal": {
            "helper": str(PRIVILEGED_REMOVAL_HELPER),
            "helper_sha256": EXPECTED_PRIVILEGED_REMOVAL_HELPER_SHA256,
            "scope": "foreign_owned_direct_candidate_directory_only",
            "fresh_full_r2_get_sha256_required": True,
            "cloudflare_canonical_pointer_match_required": True,
            "all_production_deployment_pages_checked_for_publish_in_flight": True,
            "complete_release_corpus_reference_scan_required": True,
            "root_only_quarantine_and_jsonl_journal": True,
        },
        "partial_removal_accounting": "descriptor_reconciled_files_directories_logical_allocated_estimate",
        "free_space_guard_lifecycle": "full_writer_and_removal_worker_lifetime_stop_join_all_outcomes",
        "state_snapshot_days": STATE_SNAPSHOT_RETENTION_DAYS,
        "state_snapshot_regular_files_supported": True,
        "staging_regular_files_supported": True,
        "session_spill_days": SPILL_RETENTION_DAYS,
        "cron_output_days": CRON_OUTPUT_RETENTION_DAYS,
        "cron_grouping": "direct_job_directory",
        "cron_preserve_newest_per_job": True,
        "cron_remove_directories": False,
        "checkpoint_days": CHECKPOINT_RETENTION_DAYS,
        "hardlink_aware_reclaim_accounting": True,
        "pre_remove_revalidation": [
            "device",
            "inode",
            "type",
            "size",
            "mtime_ns",
            "metadata_manifest",
            "content_manifest",
            "parent_and_source_descriptor_binding",
            "no_follow_path_components",
            "process_references",
            "authority_registry",
            "common_lock",
        ],
        "alert_above_percent": 85,
        "minimum_free_bytes": MIN_FREE_BYTES,
        "closed_lane_authority": "unavailable_fail_closed",
        "r2_checksum_and_size_verification_required_before_source_removal": True,
        "failed_upload_local_archive_policy": "delete_duplicate_only_while_source_is_retained",
    }


def _inspection_receipt(
    *,
    started_at_utc: str,
    context: dict[str, Any],
    candidates: list[dict[str, Any]],
    run_context: RunContext,
) -> dict[str, Any]:
    terminal = run_context.terminal_checkpoint()
    if terminal.site_context is not None:
        context = terminal.site_context
    results = [
        {
            "path": row["path"],
            "kind": row.get("kind"),
            "status": "DRY_RUN",
            "reason": row["reason"],
            "stats": row["stats"],
            **({"job_id": row["job_id"], "members": row["members"]} if row.get("kind") == "cron_output_batch" else {}),
        }
        for row in candidates
    ]
    exit_code = 0 if context["site_authority_valid"] else 2
    return {
        "schema": "f12-generator-archiver-inspection-v4",
        "run_id": stamp(),
        "started_at_utc": started_at_utc,
        "finished_at_utc": iso(),
        "status": "DRY_RUN" if exit_code == 0 else "DRY_RUN_SITE_AUTHORITY_BLOCKED",
        "exit_code": exit_code,
        "dry_run": True,
        "inspection_only": True,
        "receipt_path": None,
        "protected_paths": [str(path) for path in PROTECTED],
        "excluded_scan_roots": [str(path) for path in EXCLUDED_SCAN_ROOTS],
        "site_authority_valid": context["site_authority_valid"],
        "site_authority_error": context["site_authority_error"],
        "active_rollback_index": context.get("active_rollback_index_receipt"),
        "current_and_rollback_refs": sorted(str(path) for path in context["refs"]),
        "candidate_count": len(candidates),
        "results": results,
        "removed_count": 0,
        "freed_bytes": 0,
        "root_before": root_usage(),
        "root_after": root_usage(),
        "alert": None,
        "scheduled_policy": _policy_receipt(),
        "runtime": run_context.runtime_receipt(),
        "lock": {
            "path": str(run_context.lock.path),
            "owner_pid": run_context.lock.owner_pid,
            "held_through_terminal_result": True,
        },
        "effects": dict(run_context.effects),
    }


def _subdeadline_with_reserve(
    deadline: RunDeadline,
    *,
    reserve_seconds: float,
    stage: str,
) -> RunDeadline:
    remaining = deadline.remaining() - max(0.0, reserve_seconds)
    if remaining <= 0:
        raise DeadlineExceeded(f"terminal_reserve_exhausted:{stage}")
    now = time.monotonic()
    return RunDeadline(now, now + remaining, remaining)


def _raw_write_all(
    descriptor: int,
    payload: bytes,
    *,
    deadline_monotonic: float,
) -> None:
    """Deadline-aware libc write used by the terminal fallback path."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc_write = libc.write
    libc_write.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    libc_write.restype = ctypes.c_ssize_t
    original_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    fcntl.fcntl(descriptor, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
    try:
        buffer = ctypes.create_string_buffer(payload)
        offset = 0
        while offset < len(payload):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("raw_write_deadline_exhausted")
            _, writable, _ = select.select(
                [], [descriptor], [], min(WORKER_POLL_SECONDS, remaining)
            )
            if not writable:
                continue
            written = int(
                libc_write(
                    descriptor,
                    ctypes.c_void_p(ctypes.addressof(buffer) + offset),
                    len(payload) - offset,
                )
            )
            if written > 0:
                offset += written
                continue
            error_number = ctypes.get_errno()
            if written < 0 and error_number in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                continue
            raise OSError(error_number or errno.EIO, "terminal_raw_write_failed")
    finally:
        try:
            fcntl.fcntl(descriptor, fcntl.F_SETFL, original_flags)
        except OSError:
            pass


def _run_isolated_action_bounded(
    action: Callable[[], None],
    *,
    deadline: RunDeadline,
    stage: str,
) -> None:
    """Fork a killable action and reap it using WNOHANG only."""

    deadline.check(f"{stage}:before_fork")
    action_deadline = _subdeadline_with_reserve(
        deadline,
        reserve_seconds=min(0.25, deadline.remaining() / 4),
        stage=f"{stage}:cleanup_reserve",
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - behavior is parent-observed
        exit_code = 1
        try:
            os.setsid()
            action()
            exit_code = 0
        except BaseException:
            exit_code = 1
        finally:
            os._exit(exit_code)
    status: int | None = None
    try:
        while action_deadline.remaining() > 0:
            try:
                waited, candidate = os.waitpid(pid, os.WNOHANG)
            except InterruptedError:
                continue
            if waited == pid:
                status = candidate
                break
            time.sleep(min(WORKER_POLL_SECONDS, action_deadline.remaining()))
        if status is None:
            raise DeadlineExceeded(f"isolated_action_deadline_exhausted:{stage}")
    except BaseException:
        _terminate_fork_group(
            pid,
            run_context=None,
            cleanup_deadline=deadline,
        )
        raise
    if _group_exists(pid):
        _terminate_fork_group(
            pid,
            run_context=None,
            cleanup_deadline=deadline,
        )
        raise RuntimeError(f"isolated_action_descendant_survived:{stage}:{pid}")
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError(f"isolated_action_failed:{stage}")


def _serialize_json_bytes_bounded(
    value: dict[str, Any],
    *,
    deadline: RunDeadline,
    stage: str,
    max_bytes: int,
) -> bytes:
    """Serialize in a killable child while the parent continuously drains."""

    deadline.check(f"{stage}:before_serializer")
    serializer_deadline = _subdeadline_with_reserve(
        deadline,
        reserve_seconds=min(0.25, deadline.remaining() / 4),
        stage=f"{stage}:serializer_cleanup_reserve",
    )
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:  # pragma: no cover - behavior is parent-observed
        exit_code = 1
        try:
            os.setsid()
            os.close(read_fd)
            payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
            if len(payload) > max_bytes:
                exit_code = 2
            else:
                _raw_write_all(
                    write_fd,
                    payload,
                    deadline_monotonic=serializer_deadline.deadline_monotonic,
                )
                exit_code = 0
        except BaseException:
            exit_code = 1
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(exit_code)
    os.close(write_fd)
    os.set_blocking(read_fd, False)
    status: int | None = None
    pipe_eof = False
    payload = bytearray()
    try:
        while not (pipe_eof and status is not None):
            if serializer_deadline.remaining() <= 0:
                raise DeadlineExceeded(
                    f"terminal_serializer_deadline_exhausted:{stage}"
                )
            readable, _, _ = select.select(
                [read_fd] if not pipe_eof else [],
                [],
                [],
                min(WORKER_POLL_SECONDS, serializer_deadline.remaining()),
            )
            if readable:
                while True:
                    try:
                        chunk = os.read(read_fd, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        pipe_eof = True
                        break
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ValueError(
                            f"terminal_json_size_cap_exceeded:{stage}:{len(payload)}"
                        )
            if status is None:
                try:
                    waited, candidate = os.waitpid(pid, os.WNOHANG)
                except InterruptedError:
                    waited = 0
                    candidate = 0
                if waited == pid:
                    status = candidate
        if status is None or not os.WIFEXITED(status):
            raise RuntimeError(f"terminal_serializer_failed:{stage}")
        serializer_exit_code = os.WEXITSTATUS(status)
        if serializer_exit_code == 2:
            raise ValueError(
                f"terminal_json_size_cap_exceeded:{stage}:max_{max_bytes}"
            )
        if serializer_exit_code != 0:
            raise RuntimeError(f"terminal_serializer_failed:{stage}")
        return bytes(payload)
    except BaseException:
        _terminate_fork_group(
            pid,
            run_context=None,
            cleanup_deadline=deadline,
        )
        raise
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def _atomic_bytes_worker(path: Path, temporary: Path, payload: bytes) -> None:
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "atomic_receipt_short_write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _write_terminal_receipt_bounded(
    path: Path,
    value: dict[str, Any],
    *,
    deadline: RunDeadline,
) -> dict[str, Any]:
    receipt_deadline = _subdeadline_with_reserve(
        deadline,
        reserve_seconds=TERMINAL_STDOUT_RESERVE_SECONDS,
        stage="terminal_receipt_stdout_reserve",
    )
    payload = _serialize_json_bytes_bounded(
        value,
        deadline=receipt_deadline,
        stage="terminal_receipt_json",
        max_bytes=TERMINAL_RECEIPT_MAX_BYTES,
    )
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        _run_isolated_action_bounded(
            lambda: _atomic_bytes_worker(path, temporary, payload),
            deadline=receipt_deadline,
            stage="terminal_receipt_atomic_write",
        )
    except BaseException:
        if receipt_deadline.remaining() > 0 and temporary.exists():
            try:
                _run_isolated_action_bounded(
                    lambda: temporary.unlink(missing_ok=True),
                    deadline=receipt_deadline,
                    stage="terminal_receipt_partial_cleanup",
                )
            except BaseException:
                pass
        raise
    receipt_deadline.check("terminal_receipt:before_validation")
    receipt_stat = path.lstat()
    if not stat.S_ISREG(receipt_stat.st_mode) or receipt_stat.st_size != len(payload):
        raise RuntimeError("terminal_receipt_identity_or_size_mismatch")
    digest, digest_size = _bound_terminal_receipt_digest(
        path, deadline=receipt_deadline
    )
    if digest_size != receipt_stat.st_size:
        raise RuntimeError("terminal_receipt_digest_size_mismatch")
    return {
        "receipt_path": str(path),
        "receipt_sha256": digest,
        "receipt_size_bytes": receipt_stat.st_size,
    }


def _minimal_terminal_payload(
    *,
    run_context: RunContext | None,
    status: str,
    exit_code: int,
    reason_code: str,
    original_status: str,
    original_exit_code: int,
) -> bytes:
    effects = dict(run_context.effects) if run_context is not None else _zero_effects()
    safe_status = re.sub(r"[^A-Z0-9_-]", "_", status.upper())[:80] or "FAILED"
    safe_original_status = (
        re.sub(r"[^A-Za-z0-9_.:-]", "_", original_status)[:80] or "UNKNOWN"
    )
    safe_reason = re.sub(r"[^A-Za-z0-9_.:-]", "_", reason_code)[:160] or "terminal_failure"
    uncertain = bool(
        run_context is not None and run_context.effect_ledger.uncertain_scopes
    )
    removal_count = (
        sum(
            row.get("event") == "removal_effect"
            for row in run_context.effect_ledger.events
        )
        if run_context is not None
        else 0
    )
    values = {
        key: int(effects.get(key, 0))
        for key in sorted(_zero_effects())
    }
    effects_json = ",".join(f'"{key}":{value}' for key, value in values.items())
    return (
        "{"
        '"schema":"f12-f13-material-terminal-fallback-v1",'
        f'"status":"{safe_status}",'
        f'"reason_code":"{safe_reason}",'
        f'"exit_code":{int(exit_code)},'
        f'"original_status":"{safe_original_status}",'
        f'"original_exit_code":{int(original_exit_code)},'
        f'"effects_uncertain":{str(uncertain).lower()},'
        f'"removal_effect_count":{removal_count},'
        f'"effects":{{{effects_json}}}'
        "}\n"
    ).encode("ascii")


def _write_stdout_payload_bounded(
    payload: bytes,
    *,
    deadline: RunDeadline,
    stage: str,
    raw_fallback: bool,
) -> None:
    try:
        descriptor = int(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        module_name = type(sys.stdout).__module__
        if module_name.startswith("_pytest.") or isinstance(sys.stdout, io.StringIO):
            sys.stdout.write(payload.decode("utf-8", errors="replace"))
            sys.stdout.flush()
            return
        descriptor = 1

    def write_action() -> None:
        if raw_fallback:
            _raw_write_all(
                descriptor,
                payload,
                deadline_monotonic=deadline.deadline_monotonic,
            )
            return
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "terminal_stdout_short_write")
            offset += written

    _run_isolated_action_bounded(
        write_action,
        deadline=deadline,
        stage=stage,
    )


def _validated_terminal_json_max_bytes(max_bytes: int) -> int:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or max_bytes > 100_000_000
    ):
        raise ValueError("terminal_json_max_bytes_invalid")
    configured_cap = TERMINAL_RECEIPT_MAX_BYTES
    if (
        isinstance(configured_cap, bool)
        or not isinstance(configured_cap, int)
        or configured_cap <= 0
        or configured_cap != 100_000_000
    ):
        raise RuntimeError("terminal_receipt_max_bytes_invalid")
    return max_bytes


def _terminal_emit_failure_reason_code(
    fallback_reason_code: str,
    *,
    failure_stage: str,
    exc: BaseException,
) -> str:
    detail = str(exc)
    if failure_stage == "max_bytes_validation":
        category = "invalid_max_bytes"
    elif failure_stage == "serialization":
        category = (
            "json_oversize"
            if "terminal_json_size_cap_exceeded" in detail
            else "serializer_timeout"
            if isinstance(exc, DeadlineExceeded)
            else "serializer_runtime_failure"
        )
    elif failure_stage == "stdout_write":
        category = (
            "stdout_write_timeout"
            if isinstance(exc, DeadlineExceeded)
            else "stdout_write_runtime_failure"
        )
    else:
        category = "terminal_deadline_setup_failure"
    return f"{fallback_reason_code}:{category}:{type(exc).__name__}"


def emit_terminal_json_bounded(
    value: dict[str, Any],
    *,
    deadline: RunDeadline,
    run_context: RunContext | None,
    fallback_status: str,
    fallback_exit_code: int,
    fallback_reason_code: str,
    max_bytes: int = TERMINAL_STDOUT_MAX_BYTES,
) -> dict[str, Any]:
    original_status = str(value.get("status", fallback_status) or fallback_status)
    try:
        original_exit_code = int(value.get("exit_code", fallback_exit_code))
    except (TypeError, ValueError):
        try:
            original_exit_code = int(fallback_exit_code)
        except (TypeError, ValueError):
            original_exit_code = 2
    failure_stage = "max_bytes_validation"
    try:
        effective_max_bytes = _validated_terminal_json_max_bytes(max_bytes)
        failure_stage = "deadline_setup"
        normal_deadline = _subdeadline_with_reserve(
            deadline,
            reserve_seconds=TERMINAL_FALLBACK_RESERVE_SECONDS,
            stage="terminal_stdout_fallback_reserve",
        )
        failure_stage = "serialization"
        payload = _serialize_json_bytes_bounded(
            value,
            deadline=normal_deadline,
            stage="terminal_stdout_json",
            max_bytes=effective_max_bytes,
        )
        failure_stage = "stdout_write"
        _write_stdout_payload_bounded(
            payload,
            deadline=normal_deadline,
            stage="terminal_stdout_write",
            raw_fallback=False,
        )
        return {
            "full_terminal_json_emitted": True,
            "fallback_emitted": False,
            "emit_exit_code": 0,
            "emitted_status": original_status,
            "original_status": original_status,
            "original_exit_code": original_exit_code,
        }
    except BaseException as exc:
        failure_reason = _terminal_emit_failure_reason_code(
            fallback_reason_code,
            failure_stage=failure_stage,
            exc=exc,
        )
        fallback = _minimal_terminal_payload(
            run_context=run_context,
            status="FAILED_TERMINAL_STDOUT",
            exit_code=2,
            reason_code=failure_reason,
            original_status=original_status,
            original_exit_code=original_exit_code,
        )
        _write_stdout_payload_bounded(
            fallback,
            deadline=deadline,
            stage="terminal_stdout_material_fallback",
            raw_fallback=True,
        )
        return {
            "full_terminal_json_emitted": False,
            "fallback_emitted": True,
            "emit_exit_code": 2,
            "emitted_status": "FAILED_TERMINAL_STDOUT",
            "original_status": original_status,
            "original_exit_code": original_exit_code,
            "full_terminal_error": failure_reason,
        }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Legacy worker-only primitive; terminal callers use the bounded wrapper."""
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def terminal_failure_result(
    *,
    started_at_utc: str,
    inspection: bool,
    status: str,
    reason: str,
    exit_code: int,
    run_context: RunContext | None = None,
) -> dict[str, Any]:
    terminal_integrity_error: str | None = None
    if run_context is not None:
        try:
            run_context.assert_terminal_integrity()
        except BaseException as exc:
            terminal_integrity_error = f"{type(exc).__name__}:{exc}"
            status = "FAILED_TERMINAL_INTEGRITY"
            reason = f"{reason};terminal_integrity:{terminal_integrity_error}"
            exit_code = 2
    effects = dict(run_context.effects) if run_context is not None else _zero_effects()
    return {
        "schema": "f12-f13-terminal-result-v1",
        "started_at_utc": started_at_utc,
        "finished_at_utc": iso(),
        "status": status,
        "reason": reason,
        "terminal_integrity_error": terminal_integrity_error,
        "exit_code": exit_code,
        "dry_run": inspection,
        "inspection_only": inspection,
        "receipt_path": None,
        "runtime": run_context.runtime_receipt() if run_context is not None else None,
        "effects": effects,
    }


def _candidate_failure_from_effect_ledger(
    row: dict[str, Any],
    *,
    run_context: RunContext,
    status: str,
    reason: str,
) -> dict[str, Any]:
    scope = str(row["path"])
    scoped_events = run_context.effect_ledger.events_for(scope)
    removals = [
        event for event in scoped_events if event.get("event") == "removal_effect"
    ]
    removal_keys = {
        (str(event.get("relative")), str(event.get("operation")))
        for event in removals
    }
    started = [
        event for event in scoped_events if event.get("event") == "removal_started"
    ]
    unresolved_started = [
        event
        for event in started
        if (str(event.get("relative")), str(event.get("operation")))
        not in removal_keys
    ]
    uncertain = bool(
        scope in run_context.effect_ledger.uncertain_scopes or unresolved_started
    )
    if unresolved_started:
        run_context.mark_effects_uncertain(
            scope, f"generic_handler_unresolved_started:{status}"
        )
        uncertain = True
    object_removed = any(bool(event.get("object_removed")) for event in removals)
    cron_member_count = len(row.get("members") or [])
    cron_complete = bool(
        row.get("kind") == "cron_output_batch"
        and cron_member_count
        and len(removals) >= cron_member_count
        and not uncertain
    )
    source_removed = bool(object_removed and row.get("kind") != "cron_output_batch")
    any_possible_effect = bool(removals or started or uncertain)
    return {
        "path": scope,
        "kind": row.get("kind"),
        "status": status,
        "reason": reason,
        "source_retained": not any_possible_effect,
        "source_removed": source_removed,
        "selected_sources_removed": cron_complete,
        "source_partially_removed": bool(
            any_possible_effect and not source_removed and not cron_complete
        ),
        "source_state_uncertain": uncertain,
        "effects_uncertain": uncertain,
        "removed_entry_count": len(removals),
        "removed_file_count": sum(
            event.get("kind") == "regular_file" for event in removals
        ),
        "removed_directory_count": sum(
            event.get("kind") == "directory" for event in removals
        ),
        "removed_symlink_count": sum(
            event.get("kind") == "symlink" for event in removals
        ),
        "removed_logical_bytes": sum(
            int(event.get("logical_bytes", 0) or 0) for event in removals
        ),
        "removed_allocated_bytes_estimate": sum(
            int(event.get("allocated_bytes_delta", 0) or 0) for event in removals
        ),
        "reclaimed_bytes": 0,
        "reclaimed_bytes_uncertain": bool(removals or started),
        "effect_ledger_lower_bound": True,
        "started_without_completed_count": len(unresolved_started),
    }


def _deferred_candidate_rows(
    candidates: list[dict[str, Any]],
    start_index: int,
    reason: str,
) -> list[dict[str, Any]]:
    return [
        {
            "path": row["path"],
            "kind": row.get("kind"),
            "status": "DEFERRED",
            "reason": reason,
            "source_removed": False,
            "removed_file_count": 0,
            "reclaimed_bytes": 0,
        }
        for row in candidates[start_index:]
    ]


def _write_live_terminal_receipt(
    *,
    started_at_utc: str,
    run_id: str,
    context: dict[str, Any],
    candidates: list[dict[str, Any]],
    results: list[dict[str, Any]],
    before_root: dict[str, int | float],
    run_context: RunContext,
) -> tuple[dict[str, Any], int]:
    after_root = root_usage()
    removed = [
        result
        for result in results
        if result.get("source_removed") is True
        or int(result.get("removed_file_count", 0)) > 0
    ]
    material_failures = [
        result for result in results if result.get("status") in MATERIAL_FAILURE_STATUSES
    ]
    blocked = [
        result
        for result in results
        if result.get("status") in MATERIAL_FAILURE_STATUSES
    ]
    local_cleanup_failures = [
        result
        for result in results
        if (result.get("local_archive_cleanup") or {}).get("status") == "FAILED"
    ]
    exit_code = exit_code_for_results(results, dry_run=False)
    receipt_path = ARCHIVE_ROOT / f"run-{run_id}.json"
    receipt: dict[str, Any] = {
        "schema": "f12-generator-archiver-run-v5",
        "run_id": run_id,
        "started_at_utc": started_at_utc,
        "finished_at_utc": iso(),
        "status": "PARTIAL_FAILURE" if exit_code else "PASS",
        "exit_code": exit_code,
        "dry_run": False,
        "inspection_only": False,
        "receipt_path": str(receipt_path),
        "protected_paths": [str(path) for path in PROTECTED],
        "excluded_scan_roots": [str(path) for path in EXCLUDED_SCAN_ROOTS],
        "site_authority_valid": context.get("site_authority_valid", False),
        "site_authority_error": context.get("site_authority_error"),
        "active_rollback_index": context.get("active_rollback_index_receipt"),
        "current_and_rollback_refs": sorted(
            str(path) for path in context.get("refs", set())
        ),
        "candidate_count": len(candidates),
        "results": results,
        "removed_count": len(removed),
        "fully_removed_count": sum(
            1 for result in results if result.get("source_removed") is True
        ),
        "partially_removed_count": sum(
            1
            for result in results
            if result.get("source_partially_removed") is True
        ),
        "removed_file_count": sum(
            int(result.get("removed_file_count", 0)) for result in results
        ),
        "removed_directory_count": sum(
            int(result.get("removed_directory_count", 0)) for result in results
        ),
        "removed_symlink_count": sum(
            int(result.get("removed_symlink_count", 0)) for result in results
        ),
        "removed_logical_bytes": sum(
            int(result.get("removed_logical_bytes", 0)) for result in results
        ),
        "removed_allocated_bytes_estimate": sum(
            int(result.get("removed_allocated_bytes_estimate", 0))
            for result in results
        ),
        "blocked_count": len(blocked),
        "material_failure_count": len(material_failures),
        "local_archive_cleanup_failure_count": len(local_cleanup_failures),
        "offhost_verified_before_removal_count": sum(
            1
            for result in removed
            if result.get("offhost_verified_before_source_removal") is True
        ),
        "freed_bytes": sum(
            int(result.get("reclaimed_bytes", 0)) for result in removed
        ),
        "root_before": before_root,
        "root_after": after_root,
        "scheduled_policy": _policy_receipt(),
        "lock": {
            "path": str(run_context.lock.path),
            "owner_pid": run_context.lock.owner_pid,
            "held_through_terminal_receipt": True,
        },
    }
    receipt["alert"] = alert_if_needed(receipt, run_context=run_context)
    terminal = run_context.terminal_checkpoint()
    if terminal.site_context is not None:
        context = terminal.site_context
        receipt["site_authority_valid"] = context.get("site_authority_valid", False)
        receipt["site_authority_error"] = context.get("site_authority_error")
        receipt["active_rollback_index"] = context.get(
            "active_rollback_index_receipt"
        )
        receipt["current_and_rollback_refs"] = sorted(
            str(path) for path in context.get("refs", set())
        )
    run_context.effects["receipt_files_created"] = 1
    receipt["effects"] = dict(run_context.effects)
    receipt["effect_ledger"] = run_context.effect_ledger.receipt()
    receipt["runtime"] = run_context.runtime_receipt()
    terminal_deadline = _terminal_deadline_for(
        run_context.deadline, "f12_terminal_receipt"
    )
    try:
        _write_terminal_receipt_bounded(
            receipt_path,
            receipt,
            deadline=terminal_deadline,
        )
    except BaseException:
        run_context.effects["receipt_files_created"] = 0
        raise
    return receipt, exit_code


def _bound_terminal_receipt_digest(
    path: Path,
    *,
    deadline: RunDeadline | None = None,
) -> tuple[str, int]:
    _check_deadline(deadline, "terminal_receipt_digest:before_lstat")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ArchivalAborted(f"terminal_receipt_unreadable:{path}:{exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ArchivalAborted(f"terminal_receipt_regular_nonsymlink_required:{path}")
    identity = _authority_file_identity(before)
    if before.st_size > TERMINAL_RECEIPT_MAX_BYTES:
        raise ArchivalAborted(f"terminal_receipt_oversized:{path}:{before.st_size}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArchivalAborted(f"terminal_receipt_open_failed:{path}:{exc}") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        _check_deadline(deadline, "terminal_receipt_digest:before_fstat")
        opened = os.fstat(descriptor)
        if _authority_file_identity(opened) != identity:
            raise ArchivalAborted(f"terminal_receipt_identity_drift:{path}")
        while True:
            _check_deadline(deadline, "terminal_receipt_digest:read")
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > TERMINAL_RECEIPT_MAX_BYTES:
                raise ArchivalAborted(f"terminal_receipt_oversized:{path}:{size}")
            digest.update(chunk)
        _check_deadline(deadline, "terminal_receipt_digest:after_read")
        final = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise ArchivalAborted(f"terminal_receipt_identity_drift:{path}") from exc
        if (
            _authority_file_identity(final) != identity
            or _authority_file_identity(after) != identity
            or size != before.st_size
        ):
            raise ArchivalAborted(f"terminal_receipt_identity_drift:{path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def compact_live_terminal_summary(
    receipt: dict[str, Any],
    *,
    deadline: RunDeadline | None = None,
) -> dict[str, Any]:
    """Bind compact stdout to exact bytes and support both F12 and F13 schemas."""

    receipt_path = Path(str(receipt["receipt_path"]))
    receipt_sha256, receipt_size = _bound_terminal_receipt_digest(
        receipt_path, deadline=deadline
    )
    raw_results = receipt.get("results")
    results = (
        [row for row in raw_results if isinstance(row, dict)]
        if isinstance(raw_results, list)
        else []
    )
    derived_material_failures = sum(
        1 for row in results if row.get("status") in MATERIAL_FAILURE_STATUSES
    )
    derived_removed = sum(1 for row in results if row.get("source_removed") is True)
    derived_removed_files = sum(
        1
        for row in results
        if row.get("source_removed") is True and row.get("kind") == "report_file"
    )
    derived_freed = sum(
        int(row.get("reclaimed_bytes", 0) or 0)
        for row in results
        if isinstance(row.get("reclaimed_bytes", 0), int)
    )
    return {
        "schema": "f12-f13-archiver-terminal-summary-v1",
        "full_receipt_schema": receipt.get("schema"),
        "status": receipt.get("status"),
        "exit_code": int(receipt.get("exit_code", 2)),
        "run_id": receipt.get("run_id"),
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
        "receipt_size_bytes": receipt_size,
        "candidate_count": int(
            receipt["candidate_count"]
            if "candidate_count" in receipt
            else len(results)
        ),
        "removed_count": int(receipt.get("removed_count", derived_removed)),
        "removed_file_count": int(
            receipt.get("removed_file_count", derived_removed_files)
        ),
        "blocked_count": int(
            receipt.get("blocked_count", derived_material_failures)
        ),
        "material_failure_count": int(
            receipt.get("material_failure_count", derived_material_failures)
        ),
        "freed_bytes": int(receipt.get("freed_bytes", derived_freed)),
        "effects": receipt.get("effects"),
        "runtime": receipt.get("runtime"),
        "full_results_omitted_from_stdout": True,
    }


def print_compact_live_terminal(
    receipt: dict[str, Any], *, run_context: RunContext | None = None
) -> int:
    if run_context is not None:
        run_context.lock.assert_owned()
        deadline = _terminal_deadline_for(
            run_context.deadline, "compact_live_terminal_stdout"
        )
    else:
        deadline = RunDeadline.start(30.0)
    terminal_exit_code = int(receipt.get("exit_code", 2))
    try:
        summary = compact_live_terminal_summary(receipt, deadline=deadline)
    except BaseException as exc:
        summary = {
            "schema": "f12-f13-archiver-terminal-summary-failure-v1",
            "status": "FAILED_TERMINAL_SUMMARY",
            "exit_code": 2,
            "reason": f"{type(exc).__name__}:{exc}"[:500],
            "receipt_path": str(receipt.get("receipt_path") or ""),
            "effects": receipt.get("effects"),
            "runtime": receipt.get("runtime"),
            "full_results_omitted_from_stdout": True,
        }
        terminal_exit_code = 2
    emit_outcome = emit_terminal_json_bounded(
        summary,
        deadline=deadline,
        run_context=run_context,
        fallback_status=str(summary.get("status", "FAILED_TERMINAL_STDOUT")),
        fallback_exit_code=terminal_exit_code,
        fallback_reason_code="compact_live_terminal_emit_failed",
    )
    return max(terminal_exit_code, int(emit_outcome["emit_exit_code"]))


def print_terminal_result_bounded(
    receipt: dict[str, Any],
    *,
    deadline: RunDeadline,
    run_context: RunContext | None = None,
    max_bytes: int = TERMINAL_STDOUT_MAX_BYTES,
) -> int:
    source_deadline = run_context.deadline if run_context is not None else deadline
    terminal_deadline = _terminal_deadline_for(
        source_deadline, "terminal_result_stdout"
    )
    emit_outcome = emit_terminal_json_bounded(
        receipt,
        deadline=terminal_deadline,
        run_context=run_context,
        fallback_status=str(receipt.get("status", "FAILED_TERMINAL_STDOUT")),
        fallback_exit_code=int(receipt.get("exit_code", 2)),
        fallback_reason_code="terminal_result_emit_failed",
        max_bytes=max_bytes,
    )
    return int(emit_outcome["emit_exit_code"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    inspection = bool(args.dry_run or args.inspect)
    started_at_utc = iso()
    deadline = RunDeadline.start()

    try:
        token = acquire_common_lock()
    except CommonLockContention as exc:
        receipt = terminal_failure_result(
            started_at_utc=started_at_utc,
            inspection=inspection,
            status="DEFERRED_LOCK_CONTENTION",
            reason=str(exc),
            exit_code=3,
        )
        terminal_exit = print_terminal_result_bounded(receipt, deadline=deadline)
        return max(3, terminal_exit)
    except CommonLockError as exc:
        receipt = terminal_failure_result(
            started_at_utc=started_at_utc,
            inspection=inspection,
            status="FAILED_COMMON_LOCK",
            reason=str(exc),
            exit_code=2,
        )
        terminal_exit = print_terminal_result_bounded(receipt, deadline=deadline)
        return max(2, terminal_exit)

    with token:
        run_context = RunContext(token, deadline)
        run_id = stamp()
        before_root = root_usage()
        context: dict[str, Any] = {
            "site_authority_valid": False,
            "site_authority_error": "discovery_not_completed",
            "active_rollback_index_receipt": None,
            "refs": set(),
        }
        candidates: list[dict[str, Any]] = []
        try:
            if not inspection:
                ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
                install_free_space_signal()
            discovery_checkpoint = run_context.checkpoint(
                "discovery", require_site_context=True
            )
            context = discovery_checkpoint.site_context or context
            candidates = discover_candidates(
                discovery_checkpoint.processes,
                set(context["refs"]),
                site_authority_valid=bool(context["site_authority_valid"]),
                deadline=deadline,
            )
        except (DeadlineExceeded, ArchivalAborted) as exc:
            status = (
                "DEADLINE_EXHAUSTED"
                if isinstance(exc, DeadlineExceeded)
                else "ABORTED"
            )
            if inspection:
                receipt = terminal_failure_result(
                    started_at_utc=started_at_utc,
                    inspection=True,
                    status=status,
                    reason=str(exc),
                    exit_code=2,
                    run_context=run_context,
                )
                terminal_exit = print_terminal_result_bounded(
                    receipt, deadline=deadline, run_context=run_context
                )
                return max(2, terminal_exit)
            results = [
                {
                    "path": "discovery",
                    "kind": "runtime",
                    "status": status,
                    "reason": str(exc),
                    "source_removed": False,
                    "reclaimed_bytes": 0,
                }
            ]
            receipt, exit_code = _write_live_terminal_receipt(
                started_at_utc=started_at_utc,
                run_id=run_id,
                context=context,
                candidates=candidates,
                results=results,
                before_root=before_root,
                run_context=run_context,
            )
            terminal_exit = print_compact_live_terminal(
                receipt, run_context=run_context
            )
            return max(exit_code, terminal_exit)
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
            if inspection:
                receipt = terminal_failure_result(
                    started_at_utc=started_at_utc,
                    inspection=True,
                    status="FAILED",
                    reason=reason,
                    exit_code=2,
                    run_context=run_context,
                )
                terminal_exit = print_terminal_result_bounded(
                    receipt, deadline=deadline, run_context=run_context
                )
                return max(2, terminal_exit)
            results = [
                {
                    "path": "discovery",
                    "kind": "runtime",
                    "status": "FAILED",
                    "reason": reason,
                    "source_removed": False,
                    "reclaimed_bytes": 0,
                }
            ]
            try:
                receipt, exit_code = _write_live_terminal_receipt(
                    started_at_utc=started_at_utc,
                    run_id=run_id,
                    context=context,
                    candidates=candidates,
                    results=results,
                    before_root=before_root,
                    run_context=run_context,
                )
            except Exception as receipt_exc:
                terminal = terminal_failure_result(
                    started_at_utc=started_at_utc,
                    inspection=False,
                    status="FAILED_TERMINAL_RECEIPT",
                    reason=f"{reason};receipt:{type(receipt_exc).__name__}:{receipt_exc}",
                    exit_code=2,
                    run_context=run_context,
                )
                terminal_exit = print_terminal_result_bounded(
                    terminal, deadline=deadline, run_context=run_context
                )
                return max(2, terminal_exit)
            terminal_exit = print_compact_live_terminal(
                receipt, run_context=run_context
            )
            return max(exit_code, terminal_exit)

        if inspection:
            try:
                receipt = _inspection_receipt(
                    started_at_utc=started_at_utc,
                    context=context,
                    candidates=candidates,
                    run_context=run_context,
                )
            except Exception as exc:
                receipt = terminal_failure_result(
                    started_at_utc=started_at_utc,
                    inspection=True,
                    status="FAILED_TERMINAL_RESULT",
                    reason=f"{type(exc).__name__}:{exc}",
                    exit_code=2,
                    run_context=run_context,
                )
                terminal_exit = print_terminal_result_bounded(
                    receipt, deadline=deadline, run_context=run_context
                )
                return max(2, terminal_exit)
            terminal_exit = print_terminal_result_bounded(
                receipt,
                deadline=deadline,
                run_context=run_context,
                max_bytes=TERMINAL_RECEIPT_MAX_BYTES,
            )
            return max(int(receipt["exit_code"]), terminal_exit)

        archive_dir = ARCHIVE_ROOT / run_id
        results: list[dict[str, Any]] = []
        if not context["site_authority_valid"]:
            results.append(
                {
                    "path": str(_canonical_base_path()),
                    "kind": "site_authority",
                    "status": "BLOCKED",
                    "reason": f"site_authority_invalid:{context['site_authority_error']}",
                    "source_removed": False,
                    "reclaimed_bytes": 0,
                }
            )
        for candidate_index, row in enumerate(candidates):
            try:
                deadline.check("candidate:admission")
                if row.get("kind") == "cron_output_batch":
                    result = archive_file_batch(
                        row,
                        archive_dir,
                        discovery_checkpoint.processes,
                        run_context=run_context,
                    )
                else:
                    result = archive_one(
                        row,
                        archive_dir,
                        discovery_checkpoint.processes,
                        run_context=run_context,
                    )
                results.append(result)
            except DeadlineExceeded as exc:
                results.append(
                    _candidate_failure_from_effect_ledger(
                        row,
                        run_context=run_context,
                        status="DEADLINE_EXHAUSTED",
                        reason=str(exc),
                    )
                )
                results.extend(
                    _deferred_candidate_rows(
                        candidates,
                        candidate_index + 1,
                        str(exc),
                    )
                )
                break
            except ArchivalAborted as exc:
                results.append(
                    _candidate_failure_from_effect_ledger(
                        row,
                        run_context=run_context,
                        status="ABORTED",
                        reason=str(exc),
                    )
                )
                results.extend(
                    _deferred_candidate_rows(
                        candidates,
                        candidate_index + 1,
                        str(exc),
                    )
                )
                break
            except Exception as exc:
                results.append(
                    _candidate_failure_from_effect_ledger(
                        row,
                        run_context=run_context,
                        status="BLOCKED",
                        reason=f"{type(exc).__name__}:{exc}",
                    )
                )
            if deadline.remaining() <= 0:
                reason = "internal_deadline_exhausted:after_candidate"
                deadline.exhausted_stage = deadline.exhausted_stage or "after_candidate"
                results.extend(
                    _deferred_candidate_rows(
                        candidates,
                        candidate_index + 1,
                        reason,
                    )
                )
                break

        try:
            receipt, exit_code = _write_live_terminal_receipt(
                started_at_utc=started_at_utc,
                run_id=run_id,
                context=context,
                candidates=candidates,
                results=results,
                before_root=before_root,
                run_context=run_context,
            )
        except Exception as exc:
            terminal = terminal_failure_result(
                started_at_utc=started_at_utc,
                inspection=False,
                status="FAILED_TERMINAL_RECEIPT",
                reason=f"{type(exc).__name__}:{exc}",
                exit_code=2,
                run_context=run_context,
            )
            terminal_exit = print_terminal_result_bounded(
                terminal, deadline=deadline, run_context=run_context
            )
            return max(2, terminal_exit)
        terminal_exit = print_compact_live_terminal(receipt, run_context=run_context)
        return max(exit_code, terminal_exit)


if __name__ == "__main__":
    raise SystemExit(main())
