#!/usr/bin/env python3
"""Secret-safe, read-only admission gate for Hermes updates.

The gate never reads process environments or prompt/result payloads, and it
never records pane text: pane content is reduced to the derived activity
markers of ``hermes_tui_continuity.activity_from_capture`` (three booleans and
a state name) and the raw capture is discarded. The only argv values it reads
are a Hermes TUI's own ``--resume``/``--in`` options, which are session and
directory identity, never credentials.

Every durable Hermes work record is classified from a real signal — Linux PID
start ticks, the scheduler's own claim heartbeat, a newer terminal execution
row, a live turn lease, or a TUI's pane activity — so a record is either
proven live or proven stale. The gate has no "unknown work" verdict: the only
fail-closed outcome left is a source it physically could not read
(``BLOCKED_GATE_SOURCE_UNREADABLE``).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_HOME = Path(os.environ.get("HERMES_HOME", "/home/ubuntu/.hermes"))
ACTIVE_STATES = {"running", "finalizing"}
ACTIVE_EXECUTION_STATES = {"claimed", "running"}

# Mirrors the fork scheduler's own stale-claim recovery rule
# (hermes-agent cron/jobs.py:201 ONESHOT_RUN_CLAIM_TTL_SECONDS,
# cron/jobs.py:214 _oneshot_run_claim_ttl_seconds, cron/jobs.py:2844
# _claim_is_live). A healthy run heartbeats its claim well inside this window;
# a claim older than it is one the scheduler itself would recover.
# tests/test_hermes_active_work_gate.py asserts these stay equal to the fork's
# literals so the mirror cannot silently drift.
CRON_CLAIM_TTL_FALLBACK_SECONDS = 1800.0
CRON_CLAIM_TTL_HEADROOM = 3
DEFAULT_CRON_INACTIVITY_TIMEOUT = 600.0
FORK_CRON_JOBS_SOURCE = Path("/home/ubuntu/.hermes/hermes-agent/cron/jobs.py")

# Classifications. Every blocker carries exactly one of these.
ACTIVE_FINITE = "ACTIVE_FINITE"
ACTIVE_INDEFINITE = "ACTIVE_INDEFINITE"
DETACHED_SERVICE = "DETACHED_SERVICE"
STALE_PROVEN = "STALE_PROVEN"
SOURCE_UNREADABLE = "SOURCE_UNREADABLE"

TUI_IDLE = "IDLE"
TUI_ACTIVE = "ACTIVE"
SEAT_TURN_NEEDLE = "hermes_seat_turn"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cron_claim_ttl_seconds(env: dict[str, str] | None = None) -> float:
    """Resolve the scheduler's stale-claim recovery TTL the same way it does."""
    source = os.environ if env is None else env
    raw = str(source.get("HERMES_CRON_TIMEOUT", "")).strip()
    timeout = DEFAULT_CRON_INACTIVITY_TIMEOUT
    if raw:
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            timeout = DEFAULT_CRON_INACTIVITY_TIMEOUT
    if timeout <= 0:
        return float(CRON_CLAIM_TTL_FALLBACK_SECONDS)
    return max(timeout * CRON_CLAIM_TTL_HEADROOM, float(CRON_CLAIM_TTL_FALLBACK_SECONDS))


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Return Linux /proc stat field 22 without opening argv or environ."""
    try:
        raw = (proc_root / str(int(pid)) / "stat").read_text(encoding="utf-8")
        close = raw.rfind(")")
        if close < 0:
            return None
        fields_after_comm = raw[close + 2 :].split()
        return int(fields_after_comm[19])
    except (OSError, ValueError, TypeError, IndexError):
        return None


def pid_is_running(pid: Any, *, proc_root: Path = Path("/proc")) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    return (proc_root / str(pid_int)).exists()


def owner_state(pid: Any, expected_start: Any, *, proc_root: Path = Path("/proc")) -> str:
    """Classify an owner as live, stale, or unidentified using exact PID identity.

    ``unidentified`` means the RECORD names no usable owner (no pid, or start
    ticks that cannot be read), not that the host state is unknown. Callers
    resolve it against plain PID liveness, which is always knowable.
    """
    try:
        pid_int = int(pid)
        expected = int(expected_start)
    except (TypeError, ValueError):
        return "unidentified"
    if pid_int <= 0 or expected <= 0:
        return "unidentified"
    proc_dir = proc_root / str(pid_int)
    if not proc_dir.exists():
        return "stale"
    current = process_start_ticks(pid_int, proc_root=proc_root)
    if current is None:
        return "unidentified"
    return "live" if current == expected else "stale"


def _open_ro_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _state_db_paths(home: Path) -> list[Path]:
    paths = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/state.db")))
    return [path for path in paths if path.is_file()]


def _unreadable(source: Path | str, kind: str, exc: BaseException) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": str(source),
        "classification": SOURCE_UNREADABLE,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _record_owner(
    blockers: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    *,
    kind: str,
    identifier: str,
    pid: Any,
    started: Any,
    source: str,
    finite: bool,
    proc_root: Path,
    detached: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one owned record. Never yields an "unknown work" verdict.

    ``detached`` opts a record class out of blocking: a background process a
    seat explicitly did NOT ask to be notified about is a detached service, not
    a turn the update would interrupt. It is recorded, never silently dropped.
    """
    state = owner_state(pid, started, proc_root=proc_root)
    item: dict[str, Any] = {
        "kind": kind,
        "id": identifier,
        "pid": int(pid) if str(pid).isdigit() else None,
        "process_started_at": int(started) if str(started).isdigit() else None,
        "source": source,
        "owner_state": state,
    }
    if extra:
        item.update(extra)
    if state == "unidentified":
        # The record's own owner identity is incomplete. Fall back to plain PID
        # liveness, which needs no recorded start ticks.
        if pid_is_running(pid, proc_root=proc_root):
            state = "live"
            item["owner_state"] = "live_pid_start_ticks_unverified"
            item["reason"] = "pid_present_start_ticks_unverified"
        else:
            state = "stale"
            item["owner_state"] = "stale"
            item["reason"] = "no_live_owner_pid_identity"
    if state == "live":
        if detached is not None:
            item["classification"] = DETACHED_SERVICE
            item.setdefault("reason", "live_owner_not_awaited_by_a_turn")
            detached.append(item)
        else:
            item["classification"] = ACTIVE_FINITE if finite else ACTIVE_INDEFINITE
            item.setdefault("reason", "live_owner")
            blockers.append(item)
    else:
        item["classification"] = STALE_PROVEN
        item.setdefault("reason", "owner_pid_not_running")
        stale.append(item)
    return item


def inspect_state_db(
    path: Path,
    *,
    now_epoch: float,
    proc_root: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "blockers": [], "stale": [], "counts": {}, "live_session_keys": []}
    try:
        with _open_ro_sqlite(path) as conn:
            tables = _tables(conn)
            if "session_turn_leases" in tables:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(session_turn_leases)")}
                if not {"conversation_id", "expires_at"}.issubset(columns):
                    raise RuntimeError("session_turn_leases schema missing required columns")
                rows = conn.execute(
                    "SELECT conversation_id, expires_at FROM session_turn_leases WHERE expires_at > ?",
                    (now_epoch,),
                ).fetchall()
                result["counts"]["unexpired_turn_leases"] = len(rows)
                for row in rows:
                    conversation = str(row["conversation_id"])
                    result["live_session_keys"].append(conversation)
                    result["blockers"].append({
                        "kind": "session_turn_lease",
                        "id": conversation,
                        "source": str(path),
                        "classification": ACTIVE_FINITE,
                        "reason": "unexpired_turn_lease",
                        "expires_at": float(row["expires_at"]),
                    })
            if "async_delegations" in tables:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(async_delegations)")}
                required = {"delegation_id", "state", "owner_pid", "owner_started_at"}
                if not required.issubset(columns):
                    raise RuntimeError("async_delegations schema missing required columns")
                rows = conn.execute(
                    "SELECT delegation_id, state, owner_pid, owner_started_at "
                    "FROM async_delegations WHERE state IN ('running','finalizing')"
                ).fetchall()
                result["counts"]["nonterminal_delegations"] = len(rows)
                for row in rows:
                    _record_owner(
                        result["blockers"], result["stale"],
                        kind="async_delegation",
                        identifier=str(row["delegation_id"]),
                        pid=row["owner_pid"],
                        started=row["owner_started_at"],
                        source=str(path),
                        finite=True,
                        proc_root=proc_root,
                    )
    except Exception as exc:
        result["blockers"].append(_unreadable(path, "state_db", exc))
    return result


def parse_claim_owner(value: Any) -> tuple[str, int | None]:
    """Split the scheduler's ``host:pid[:token]`` claim owner (cron/jobs.py)."""
    text = str(value or "")
    parts = text.split(":")
    if len(parts) < 2:
        return text, None
    try:
        return parts[0], int(parts[1])
    except (TypeError, ValueError):
        return parts[0], None


def claim_age_seconds(claim: Any, now_epoch: float) -> float | None:
    if not isinstance(claim, dict):
        return None
    raw = claim.get("at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now_epoch - parsed.timestamp()


def classify_claim(
    claim: Any,
    *,
    now_epoch: float,
    ttl_seconds: float,
    proc_root: Path,
    local_host: str,
) -> dict[str, Any]:
    """Classify one cron claim from the scheduler's own liveness semantics.

    A claim is held by ``host:pid`` and heartbeated while its run lives. So:
    an owner PID that is gone proves the claim stale, and a claim older than
    the scheduler's own recovery TTL proves the heartbeat stopped. Anything
    else is live, finite work the gate can wait for.
    """
    owner_host, owner_pid = parse_claim_owner((claim or {}).get("by") if isinstance(claim, dict) else None)
    age = claim_age_seconds(claim, now_epoch)
    verdict: dict[str, Any] = {
        "owner_host": owner_host,
        "owner_pid": owner_pid,
        "age_seconds": None if age is None else round(age, 3),
        "ttl_seconds": ttl_seconds,
    }
    foreign_host = bool(owner_host) and owner_host != local_host
    if age is not None and age >= ttl_seconds:
        verdict.update({"classification": STALE_PROVEN, "reason": "claim_older_than_scheduler_ttl"})
        return verdict
    if not foreign_host and not pid_is_running(owner_pid, proc_root=proc_root):
        verdict.update({"classification": STALE_PROVEN, "reason": "claim_owner_pid_not_running"})
        return verdict
    if age is None:
        verdict.update({"classification": ACTIVE_FINITE, "reason": "live_owner_unparseable_claim_timestamp"})
        return verdict
    if foreign_host:
        verdict.update({"classification": ACTIVE_FINITE, "reason": "fresh_claim_from_other_host"})
        return verdict
    verdict.update({"classification": ACTIVE_FINITE, "reason": "live_owner_within_ttl"})
    return verdict


def inspect_cron_claims(
    path: Path,
    *,
    now_epoch: float | None = None,
    ttl_seconds: float | None = None,
    proc_root: Path = Path("/proc"),
    local_host: str | None = None,
) -> dict[str, Any]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    ttl_seconds = cron_claim_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
    local_host = socket.gethostname() if local_host is None else local_host
    result: dict[str, Any] = {
        "path": str(path),
        "blockers": [],
        "stale": [],
        "counts": {"claims": 0},
        "live_claim_job_ids": [],
    }
    if not path.is_file():
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        jobs: Iterable[Any]
        if isinstance(payload, list):
            jobs = payload
        elif isinstance(payload, dict):
            jobs = payload.get("jobs") or []
        else:
            raise RuntimeError("jobs JSON root is not an object or list")
        for job in jobs:
            if not isinstance(job, dict):
                raise RuntimeError("jobs JSON contains a non-object job")
            job_id = str(job.get("id") or "unknown")
            for field in ("run_claim", "fire_claim"):
                claim = job.get(field)
                if not claim:
                    continue
                result["counts"]["claims"] += 1
                verdict = classify_claim(
                    claim,
                    now_epoch=now_epoch,
                    ttl_seconds=ttl_seconds,
                    proc_root=proc_root,
                    local_host=local_host,
                )
                item = {
                    "kind": "cron_claim",
                    "id": job_id,
                    "claim_kind": field,
                    "source": str(path),
                    **verdict,
                }
                if verdict["classification"] == ACTIVE_FINITE:
                    result["live_claim_job_ids"].append(job_id)
                    result["blockers"].append(item)
                else:
                    result["stale"].append(item)
    except Exception as exc:
        result["blockers"].append(_unreadable(path, "cron_jobs_json", exc))
    return result


def inspect_cron_executions(
    path: Path,
    *,
    proc_root: Path,
    live_claim_job_ids: set[str] | None = None,
    now_epoch: float | None = None,
    ttl_seconds: float | None = None,
) -> dict[str, Any]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    ttl_seconds = cron_claim_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
    live_claims = set(live_claim_job_ids or ())
    result: dict[str, Any] = {"path": str(path), "blockers": [], "stale": [], "counts": {}}
    if not path.is_file():
        return result
    try:
        with _open_ro_sqlite(path) as conn:
            tables = _tables(conn)
            if "executions" not in tables:
                raise RuntimeError("executions table missing")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(executions)")}
            required = {"id", "job_id", "pid", "process_started_at", "status"}
            if not required.issubset(columns):
                raise RuntimeError("executions schema missing required columns")
            select = ["id", "job_id", "pid", "process_started_at", "status"]
            for optional in ("handoff_pending", "claimed_at", "started_at"):
                if optional in columns:
                    select.append(optional)
            rows = conn.execute(
                f"SELECT {', '.join(select)} FROM executions WHERE status IN ('claimed','running')"
            ).fetchall()
            result["counts"]["nonterminal_executions"] = len(rows)
            newest_terminal: dict[str, str] = {}
            if "claimed_at" in columns:
                for row in conn.execute(
                    "SELECT job_id, MAX(claimed_at) AS newest FROM executions "
                    "WHERE status NOT IN ('claimed','running') GROUP BY job_id"
                ).fetchall():
                    newest_terminal[str(row["job_id"])] = str(row["newest"] or "")
            for row in rows:
                job_id = str(row["job_id"])
                keys = row.keys()
                claimed_at = str(row["claimed_at"]) if "claimed_at" in keys and row["claimed_at"] else ""
                extra: dict[str, Any] = {"job_id": job_id}
                if "handoff_pending" in keys and bool(row["handoff_pending"]):
                    extra["handoff_pending"] = True
                if claimed_at:
                    extra["claimed_at"] = claimed_at
                state = owner_state(row["pid"], row["process_started_at"], proc_root=proc_root)
                live_owner = state == "live" or (
                    state == "unidentified" and pid_is_running(row["pid"], proc_root=proc_root)
                )
                if not live_owner:
                    _record_owner(
                        result["blockers"], result["stale"],
                        kind="cron_execution",
                        identifier=str(row["id"]),
                        pid=row["pid"],
                        started=row["process_started_at"],
                        source=str(path),
                        finite=True,
                        proc_root=proc_root,
                        extra=extra,
                    )
                    continue
                # The owner PID is alive, but for an in-gateway job that PID is
                # the long-lived gateway daemon and can never go stale on its
                # own. Corroborate against signals that DO expire: the job's own
                # heartbeated claim, a newer terminal execution for the same
                # job, and the scheduler's recovery TTL.
                item = {
                    "kind": "cron_execution",
                    "id": str(row["id"]),
                    "pid": int(row["pid"]) if str(row["pid"]).isdigit() else None,
                    "process_started_at": int(row["process_started_at"]) if str(row["process_started_at"]).isdigit() else None,
                    "source": str(path),
                    "owner_state": "live",
                    **extra,
                }
                superseded = bool(claimed_at and newest_terminal.get(job_id, "") > claimed_at)
                age = None
                if claimed_at:
                    try:
                        parsed = datetime.fromisoformat(claimed_at)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        age = now_epoch - parsed.timestamp()
                    except (TypeError, ValueError):
                        age = None
                if job_id in live_claims:
                    item.update({"classification": ACTIVE_FINITE, "reason": "job_claim_heartbeat_is_live"})
                    result["blockers"].append(item)
                elif superseded:
                    item.update({"classification": STALE_PROVEN, "reason": "superseded_by_newer_terminal_execution"})
                    result["stale"].append(item)
                elif age is not None and age >= ttl_seconds:
                    item.update({
                        "classification": STALE_PROVEN,
                        "reason": "no_live_job_claim_past_scheduler_ttl",
                        "age_seconds": round(age, 3),
                    })
                    result["stale"].append(item)
                else:
                    item.update({"classification": ACTIVE_FINITE, "reason": "recent_execution_with_live_owner"})
                    result["blockers"].append(item)
    except Exception as exc:
        result["blockers"].append(_unreadable(path, "cron_executions_db", exc))
    return result


def inspect_process_registry(
    path: Path,
    *,
    proc_root: Path,
    live_session_keys: set[str] | None = None,
) -> dict[str, Any]:
    live_sessions = set(live_session_keys or ())
    result: dict[str, Any] = {
        "path": str(path),
        "blockers": [],
        "stale": [],
        "detached": [],
        "counts": {"active_rows": 0},
    }
    if not path.is_file():
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("processes") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("process registry root is not a list")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("process registry contains a non-object row")
            status = str(row.get("status") or "").lower()
            if row.get("exited") is True or status in {"exited", "completed", "failed", "killed"}:
                continue
            result["counts"]["active_rows"] += 1
            identifier = str(row.get("session_id") or "unknown")
            session_key = str(row.get("session_key") or row.get("owner_task_id") or "")
            scope = str(row.get("pid_scope") or "host")
            awaited = bool(row.get("notify_on_complete"))
            if scope != "host":
                # This host's /proc cannot see that PID. The record is still
                # classifiable: it belongs to a session, and that session is
                # either live here or it is not.
                item = {
                    "kind": "managed_process",
                    "id": identifier,
                    "source": str(path),
                    "pid_scope": scope,
                    "session_key": session_key,
                }
                if session_key and session_key in live_sessions:
                    item.update({
                        "classification": ACTIVE_INDEFINITE,
                        "reason": "unverifiable_pid_scope_with_live_owning_session",
                    })
                    result["blockers"].append(item)
                else:
                    item.update({
                        "classification": STALE_PROVEN,
                        "reason": "unverifiable_pid_scope_without_live_owning_session",
                    })
                    result["stale"].append(item)
                continue
            _record_owner(
                result["blockers"], result["stale"],
                kind="managed_process",
                identifier=identifier,
                pid=row.get("pid"),
                started=row.get("host_start_time"),
                source=str(path),
                finite=awaited,
                proc_root=proc_root,
                # notify_on_complete is the seat's own statement about whether a
                # turn is waiting on this process. When it is false the process
                # is a detached service (a preview server, a watcher) that never
                # ends by itself; blocking on it made the gate permanently
                # un-passable while proving nothing about in-flight work.
                detached=None if awaited else result["detached"],
                extra={"session_key": session_key, "notify_on_complete": awaited},
            )
    except Exception as exc:
        result["blockers"].append(_unreadable(path, "process_registry", exc))
    return result


def inspect_active_session_registry(path: Path, *, proc_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "blockers": [], "stale": [], "counts": {"rows": 0}}
    if not path.is_file():
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise RuntimeError("active-session registry root is not a list")
        result["counts"]["rows"] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("active-session registry contains a non-object row")
            # Older registries use wall-clock process_start_time rather than
            # kernel ticks. A missing tick value no longer blocks: _record_owner
            # falls back to plain PID liveness, which is always knowable.
            ticks = row.get("process_start_ticks") or row.get("host_start_time")
            _record_owner(
                result["blockers"], result["stale"],
                kind="active_session",
                identifier=str(row.get("lease_id") or row.get("session_id") or "unknown"),
                pid=row.get("pid"),
                started=ticks,
                source=str(path),
                finite=True,
                proc_root=proc_root,
            )
    except Exception as exc:
        result["blockers"].append(_unreadable(path, "active_session_registry", exc))
    return result


# --------------------------------------------------------------------------
# Hermes TUI census
# --------------------------------------------------------------------------

def _read_cmdline(pid: int, *, proc_root: Path) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _argv_option(argv: list[str], flag: str) -> str:
    try:
        index = argv.index(flag)
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def is_hermes_tui_argv(argv: list[str]) -> bool:
    """True for both invocation shapes: ``hermes --tui`` and ``hermes chat --tui``."""
    if "--tui" not in argv:
        return False
    return any(
        Path(value).name in {"hermes", "hermes.py"} or value.endswith("/hermes")
        for value in argv
    )


def _proc_parents(proc_root: Path) -> dict[int, int]:
    parents: dict[int, int] = {}
    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        try:
            raw = (child / "stat").read_text(encoding="utf-8")
            close = raw.rfind(")")
            if close < 0:
                continue
            parents[int(child.name)] = int(raw[close + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return parents


def _descends_from(pid: int, ancestor: int, parents: dict[int, int]) -> bool:
    seen: set[int] = set()
    current = pid
    while current and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parents.get(current, 0)
    return False


def hermes_tui_processes(*, proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    """Enumerate live Hermes TUI roots, reading only --resume/--in from argv."""
    found: list[dict[str, Any]] = []
    try:
        entries = sorted(child for child in proc_root.iterdir() if child.name.isdigit())
    except OSError:
        return found
    for child in entries:
        pid = int(child.name)
        argv = _read_cmdline(pid, proc_root=proc_root)
        if not is_hermes_tui_argv(argv):
            continue
        found.append({
            "pid": pid,
            "resume_id": _argv_option(argv, "--resume"),
            "workdir": _argv_option(argv, "--in"),
            "start_ticks": process_start_ticks(pid, proc_root=proc_root),
            "argv_shape": "chat" if "chat" in argv else "flag",
        })
    return found


def _seat_turn_pids(*, proc_root: Path = Path("/proc")) -> list[int]:
    pids: list[int] = []
    try:
        entries = sorted(child for child in proc_root.iterdir() if child.name.isdigit())
    except OSError:
        return pids
    for child in entries:
        pid = int(child.name)
        argv = _read_cmdline(pid, proc_root=proc_root)
        if any(SEAT_TURN_NEEDLE in part for part in argv):
            pids.append(pid)
    return pids


def _pane_map(runner: Callable[..., Any] | None = None) -> dict[int, dict[str, str]]:
    """Map pane_pid → pane identity using tmux metadata only."""
    import subprocess

    command = ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}"]
    try:
        if runner is None:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=20)
        else:
            proc = runner(command, timeout=20)
    except Exception:
        return {}
    if getattr(proc, "returncode", 1) != 0:
        return {}
    panes: dict[int, dict[str, str]] = {}
    for line in (proc.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[2].isdigit():
            continue
        panes[int(fields[2])] = {"session_name": fields[0], "pane_id": fields[1]}
    return panes


_ACTIVITY_CLASSIFIER: Callable[[str], dict[str, Any]] | None = None


def _activity_classifier() -> Callable[[str], dict[str, Any]]:
    """Load hermes_tui_continuity.activity_from_capture once, by exact path."""
    global _ACTIVITY_CLASSIFIER
    if _ACTIVITY_CLASSIFIER is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hermes_tui_continuity_activity",
            Path(__file__).resolve().parent / "hermes_tui_continuity.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register before exec: dataclasses resolves cls.__module__ through
        # sys.modules, and a module loaded by path alone fails without it.
        import sys as _sys

        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ACTIVITY_CLASSIFIER = module.activity_from_capture
    return _ACTIVITY_CLASSIFIER


def _pane_activity(pane_id: str, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Derived activity markers only. The raw capture is never returned."""
    import subprocess

    command = ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-16"]
    try:
        if runner is None:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=20)
        else:
            proc = runner(command, timeout=20)
        if getattr(proc, "returncode", 1) != 0:
            return {"state": "capture_failed", "evidence": {"returncode": getattr(proc, "returncode", None)}}
        return _activity_classifier()(proc.stdout or "")
    except Exception as exc:
        return {"state": "capture_failed", "evidence": {"error": type(exc).__name__}}


def tui_census(
    *,
    proc_root: Path = Path("/proc"),
    live_session_keys: set[str] | None = None,
    tmux_runner: Callable[..., Any] | None = None,
    capture_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Classify every live Hermes TUI as IDLE or ACTIVE from real signals.

    Signals, in order of authority: a live ``hermes_seat_turn`` descendant, an
    unexpired turn lease for the TUI's own session, and the pane's own activity
    markers. There is no UNKNOWN verdict: a TUI with no live turn signal is
    IDLE, and a TUI that shows one is ACTIVE and is waited for, never killed.
    """
    leases = set(live_session_keys or ())
    parents = _proc_parents(proc_root)
    seat_turns = _seat_turn_pids(proc_root=proc_root)
    panes = _pane_map(tmux_runner)
    census: list[dict[str, Any]] = []
    for proc in hermes_tui_processes(proc_root=proc_root):
        pid = int(proc["pid"])
        # The TUI is not always the pane's direct process. The updater relaunches
        # each seat as `bash -lc '... hermes --tui ...'`, so the pane pid is the
        # WRAPPER and a direct pid lookup silently yields no session_name - the
        # census then reports five nameless TUIs. Walk up to the first ancestor
        # that owns a pane.
        pane = panes.get(pid, {})
        if not pane:
            walker, seen = pid, 0
            while walker and walker > 1 and seen < 12:
                walker = parents.get(walker, 0)
                seen += 1
                if walker in panes:
                    pane = panes[walker]
                    break
        owned_turns = [turn for turn in seat_turns if _descends_from(turn, pid, parents)]
        lease_hit = bool(proc["resume_id"]) and proc["resume_id"] in leases
        activity = _pane_activity(pane["pane_id"], capture_runner) if pane.get("pane_id") else {
            "state": "no_pane", "evidence": {}
        }
        pane_active = activity.get("state") == "active"
        state = TUI_ACTIVE if (owned_turns or lease_hit or pane_active) else TUI_IDLE
        census.append({
            "pid": pid,
            "session_name": pane.get("session_name", ""),
            "pane_id": pane.get("pane_id", ""),
            "resume_id": proc["resume_id"],
            "workdir": proc["workdir"],
            "argv_shape": proc["argv_shape"],
            "state": state,
            "signals": {
                "seat_turn_pids": owned_turns,
                "unexpired_turn_lease": lease_hit,
                "pane_activity_state": activity.get("state"),
                "pane_activity_evidence": activity.get("evidence", {}),
            },
        })
    return {
        "tuis": census,
        "tui_count": len(census),
        "tui_active_count": sum(1 for row in census if row["state"] == TUI_ACTIVE),
        "tui_idle_count": sum(1 for row in census if row["state"] == TUI_IDLE),
        "unclassified_count": sum(1 for row in census if row["state"] not in {TUI_IDLE, TUI_ACTIVE}),
    }


def collect_active_work(
    *,
    home: Path = DEFAULT_HOME,
    proc_root: Path = Path("/proc"),
    now_epoch: float | None = None,
    include_tui_census: bool = True,
    tmux_runner: Callable[..., Any] | None = None,
    capture_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    ttl_seconds = cron_claim_ttl_seconds()
    local_host = socket.gethostname()
    observations: list[dict[str, Any]] = []
    live_session_keys: set[str] = set()
    for path in _state_db_paths(home):
        observation = inspect_state_db(path, now_epoch=now_epoch, proc_root=proc_root)
        live_session_keys.update(observation.get("live_session_keys") or [])
        observations.append(observation)
    claims = inspect_cron_claims(
        home / "cron" / "jobs.json",
        now_epoch=now_epoch,
        ttl_seconds=ttl_seconds,
        proc_root=proc_root,
        local_host=local_host,
    )
    observations.extend([
        inspect_cron_executions(
            home / "cron" / "executions.db",
            proc_root=proc_root,
            live_claim_job_ids=set(claims.get("live_claim_job_ids") or ()),
            now_epoch=now_epoch,
            ttl_seconds=ttl_seconds,
        ),
        claims,
        inspect_process_registry(
            home / "processes.json",
            proc_root=proc_root,
            live_session_keys=live_session_keys,
        ),
        inspect_active_session_registry(home / "runtime" / "active_sessions.json", proc_root=proc_root),
    ])
    blockers = [item for observation in observations for item in observation.get("blockers", [])]
    stale = [item for observation in observations for item in observation.get("stale", [])]
    detached = [item for observation in observations for item in observation.get("detached", [])]
    unreadable = [item for item in blockers if item.get("classification") == SOURCE_UNREADABLE]
    active = [item for item in blockers if str(item.get("classification", "")).startswith("ACTIVE_")]
    active_finite = [item for item in active if item.get("classification") == ACTIVE_FINITE]
    active_indefinite = [item for item in active if item.get("classification") == ACTIVE_INDEFINITE]

    census = (
        tui_census(
            proc_root=proc_root,
            live_session_keys=live_session_keys,
            tmux_runner=tmux_runner,
            capture_runner=capture_runner,
        )
        if include_tui_census
        else {"tuis": [], "tui_count": 0, "tui_active_count": 0, "tui_idle_count": 0, "unclassified_count": 0}
    )
    active_tuis = [row for row in census["tuis"] if row["state"] == TUI_ACTIVE]

    if unreadable:
        status = "BLOCKED_GATE_SOURCE_UNREADABLE"
    elif active_indefinite:
        status = "BLOCKED_ACTIVE_HERMES_WORK"
    elif active_finite or active_tuis:
        # Finite work and a mid-turn seat are both waitable: the caller either
        # waits for them or records a clean skip. Neither is ever killed.
        status = "SKIPPED_ACTIVE_HERMES_WORK"
    else:
        status = "IDLE_SAFE"
    return {
        "schema_version": 2,
        "checked_at": utc_now(),
        "ok": not blockers and not active_tuis,
        "status": status,
        "active_count": len(active) + len(active_tuis),
        "active_finite_count": len(active_finite) + len(active_tuis),
        "active_indefinite_count": len(active_indefinite),
        "unknown_count": 0,
        "unreadable_source_count": len(unreadable),
        "stale_count": len(stale),
        "detached_count": len(detached),
        "blockers": blockers,
        "stale_proven": stale,
        "detached_services": detached,
        "observations": observations,
        "tui_census": census,
        "cron_claim_ttl_seconds": ttl_seconds,
        "host": local_host,
        "secret_safe": True,
        "forbidden_sources_read": [],
    }


def wait_for_idle(
    *,
    home: Path = DEFAULT_HOME,
    proc_root: Path = Path("/proc"),
    timeout_seconds: float = 0,
    poll_seconds: float = 2,
    include_tui_census: bool = True,
) -> dict[str, Any]:
    """Wait out finite work and mid-turn seats; never wait on a hard block."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    waited_polls = 0
    started = time.monotonic()
    while True:
        result = collect_active_work(
            home=home, proc_root=proc_root, include_tui_census=include_tui_census
        )
        result["waited_seconds"] = round(time.monotonic() - started, 3)
        result["wait_budget_seconds"] = float(timeout_seconds)
        result["waited_polls"] = waited_polls
        if result["ok"]:
            return result
        if result["status"] in {"BLOCKED_GATE_SOURCE_UNREADABLE", "BLOCKED_ACTIVE_HERMES_WORK"}:
            return result
        if time.monotonic() >= deadline:
            return result
        waited_polls += 1
        time.sleep(max(0.05, poll_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--wait", type=float, default=0, help="wait this many seconds for finite work")
    parser.add_argument("--poll", type=float, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-tui-census", action="store_true")
    args = parser.parse_args(argv)
    result = wait_for_idle(
        home=args.home,
        timeout_seconds=args.wait,
        poll_seconds=args.poll,
        include_tui_census=not args.no_tui_census,
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, args.output)
    print(encoded, end="")
    if result["ok"] or result["status"] == "SKIPPED_ACTIVE_HERMES_WORK":
        return 0
    return 2 if result["unreadable_source_count"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
