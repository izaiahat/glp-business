#!/usr/bin/env python3
"""
hermes_state_archive/archive.py — staged, receipted archive tool for ~/.hermes/state.db

Implements the procedure specified in
  business/reports/ops/orchestration-restore-20260907/PLAN/Q34/MEASUREMENT.md
  sections 7(b) [archive plan], 7(b5) [quiet-window preconditions], 7(c) [prune-script
  defects], 7(d) [proof protocol] and "Risk: could the archive break /resume".

Design rules baked in (do not relax without re-reading the measurement):

  * READ-ONLY subcommands (plan, preflight, verify, report) open the database with
    ``file:<path>?mode=ro`` + ``PRAGMA query_only=1`` and never hold a read transaction
    open across steps.  A slow reader defeats every WAL checkpoint (MEASUREMENT §5.1b),
    so every live read is chunked and bounded.
  * EFFECTFUL subcommands (backup, copy, delete, rebuild-fts, drop-fts-triggers,
    restore-fts-triggers, vacuum) refuse to run without --i-am-in-the-quiet-window and
    always write a receipt.
  * The doomed set is computed ONCE by ``plan`` and pinned in PLAN.json.  ``copy``,
    ``verify`` and ``delete`` all read that pinned set.  ``delete`` never recomputes a
    fresh cutoff — it recomputes only *which of the pinned sessions still have rows*.
  * Nothing here ever signals a process.

Every subcommand accepts --db so the test-suite can point at a fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

TOOL_VERSION = "1.0.0"
PLAN_SCHEMA_VERSION = 1

DEFAULT_DB = "/home/ubuntu/.hermes/state.db"
DEFAULT_CONFIG = "/home/ubuntu/.hermes/config.yaml"

# Tables moved into the archive (MEASUREMENT §7b "Tables to move").
ARCHIVE_TABLES = ("sessions", "messages", "session_model_usage")

# The two FTS5 external-content indexes over `messages`.
FTS_INDEXES = ("messages_fts", "messages_fts_trigram")

# The six triggers on `messages` that keep both FTS indexes in sync.
FTS_TRIGGERS = (
    "messages_fts_insert",
    "messages_fts_update",
    "messages_fts_delete",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_update",
    "messages_fts_trigram_delete",
)

# Payload columns used for the byte accounting (identical list to MEASUREMENT §2.3).
PAYLOAD_COLUMNS = (
    "content",
    "tool_calls",
    "reasoning",
    "reasoning_details",
    "codex_reasoning_items",
    "reasoning_content",
    "codex_message_items",
    "api_content",
    "display_metadata",
    "tool_name",
    "role",
    "session_id",
)

GIB = 1024 ** 3
TWO_GIB = 2 * GIB

QUIET_FLAG = "--i-am-in-the-quiet-window"

EFFECTFUL = {
    "backup",
    "copy",
    "delete",
    "rebuild-fts",
    "drop-fts-triggers",
    "restore-fts-triggers",
    "vacuum",
}

# Process-name patterns that indicate a live Hermes process (MEASUREMENT §7c defect c1:
# `pgrep -f "hermes chat --tui"` does NOT match the real backends).
HERMES_PROC_PATTERNS = (
    "tui_gateway.entry",
    "tui_gateway.slash_worker",
    "hermes_cli.main gateway run",
    "hermes_cli.main",
    "hermes chat",
)
GATEWAY_PROC_PATTERN = "hermes_cli.main gateway run"


class PreconditionFailed(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


def die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[valid-type]
    sys.stderr.write("ERROR: %s\n" % msg)
    raise SystemExit(code)


def log(msg: str) -> None:
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%SZ", time.gmtime()), msg))
    sys.stderr.flush()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# --------------------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------------------


def connect_ro(path: str, timeout: float = 5.0) -> sqlite3.Connection:
    """Open strictly read-only.  Both belt (mode=ro) and braces (query_only=1)."""
    if not os.path.exists(path):
        die("database does not exist: %s" % path)
    uri = "file:%s?mode=ro" % _uri_path(path)
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=1")
    return conn


def connect_rw(path: str, quiet_window: bool, timeout: float = 10.0) -> sqlite3.Connection:
    if not quiet_window:
        die("refusing to open %s for writing without %s" % (path, QUIET_FLAG))
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    # Match the Hermes writer's WAL ceiling so we never strand a giant -wal
    # (_WAL_SIZE_LIMIT_BYTES = 64 MiB, hermes_state.py:955 / 1179-1215).
    conn.execute("PRAGMA journal_size_limit=%d" % (64 * 1024 * 1024))
    return conn


def _uri_path(path: str) -> str:
    p = os.path.abspath(path)
    return p.replace("?", "%3f").replace("#", "%23")


def timed(label: str, fn, timings: Optional[Dict[str, float]] = None):
    t0 = time.time()
    out = fn()
    dt = time.time() - t0
    if timings is not None:
        timings[label] = round(dt, 3)
    log("%-42s %8.3f s" % (label, dt))
    return out


# --------------------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------------------


def write_receipt(receipts_dir: str, subcommand: str, payload: Dict[str, Any]) -> str:
    os.makedirs(receipts_dir, exist_ok=True)
    payload = dict(payload)
    payload.setdefault("tool_version", TOOL_VERSION)
    payload.setdefault("subcommand", subcommand)
    payload.setdefault("written_at", iso(time.time()))
    payload.setdefault("argv", sys.argv[1:])
    path = os.path.join(receipts_dir, "%s-%s.json" % (now_stamp(), subcommand))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    log("receipt: %s" % path)
    return path


def latest_receipt(receipts_dir: str, subcommand: str) -> Optional[Dict[str, Any]]:
    if not os.path.isdir(receipts_dir):
        return None
    names = sorted(n for n in os.listdir(receipts_dir) if n.endswith("-%s.json" % subcommand))
    if not names:
        return None
    with open(os.path.join(receipts_dir, names[-1])) as fh:
        return json.load(fh)


def sha256_file(path: str, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# schema helpers
# --------------------------------------------------------------------------------------


def object_sql(conn: sqlite3.Connection, name: str) -> Optional[str]:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name IN (%s) ORDER BY name" % ",".join("?" * len(ARCHIVE_TABLES)),
        ARCHIVE_TABLES,
    ).fetchall()
    h = hashlib.sha256()
    for t, n, s in rows:
        h.update(("%s\x00%s\x00%s\x00" % (t, n, s)).encode("utf-8"))
    return h.hexdigest()


def rewrite_create_table(ddl: str, table: str, schema: str = "arch") -> str:
    """Take the verbatim CREATE TABLE text from sqlite_master and target `schema`.

    Only the table name is rewritten; every column, default, constraint and FK is left
    byte-identical (MEASUREMENT §7b2: "copy the CREATE TABLE text verbatim").
    """
    pattern = re.compile(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\"%(t)s\"|'%(t)s'|\[%(t)s\]|`%(t)s`|%(t)s)" % {"t": re.escape(table)},
        re.IGNORECASE,
    )
    m = pattern.match(ddl)
    if not m:
        die("could not rewrite CREATE TABLE for %r; DDL starts: %r" % (table, ddl[:120]))
    return 'CREATE TABLE %s."%s"' % (schema, table) + ddl[m.end():]


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table).fetchall()]


# --------------------------------------------------------------------------------------
# the doomed set  (MEASUREMENT §7b2 + "Risk" 1-5)
# --------------------------------------------------------------------------------------


@dataclass
class SessionRow:
    id: str
    parent: Optional[str]
    pinned: int
    last_activity_at: Optional[float]
    ended_at: Optional[float]
    started_at: float
    msg_count: int = 0
    msg_max_ts: Optional[float] = None
    msg_min_ts: Optional[float] = None


@dataclass
class DoomedResult:
    cutoff: float
    sessions: List[SessionRow]
    doomed_ids: List[str]
    kept_ids: List[str]
    excluded: Dict[str, int] = field(default_factory=dict)
    lineage_rescued: List[str] = field(default_factory=list)
    lineage_iterations: int = 0
    timings: Dict[str, float] = field(default_factory=dict)


def load_sessions(conn: sqlite3.Connection, timings: Dict[str, float]) -> List[SessionRow]:
    rows = timed(
        "q:sessions",
        lambda: conn.execute(
            "SELECT id, parent_session_id, COALESCE(pinned,0), last_activity_at, "
            "       ended_at, started_at "
            "FROM sessions"
        ).fetchall(),
        timings,
    )
    return [
        SessionRow(id=r[0], parent=r[1], pinned=int(r[2] or 0),
                   last_activity_at=r[3], ended_at=r[4], started_at=r[5])
        for r in rows
    ]


def load_message_extents(conn: sqlite3.Connection, timings: Dict[str, float]
                         ) -> Dict[str, Tuple[int, float, float]]:
    """One index-only pass over idx_messages_session(session_id, timestamp).

    MEASUREMENT §2.2 measured this at 4.86 s over 2.1 M rows; it replaces the
    correlated `(SELECT max(m.timestamp) ... )` subquery of §7b2, which would be a
    per-session seek.  Falls back to an unhinted GROUP BY when the index is absent
    (i.e. on a fixture that did not create it).
    """
    sql_hinted = ("SELECT session_id, count(*), max(timestamp), min(timestamp) "
                  "FROM messages INDEXED BY idx_messages_session GROUP BY session_id")
    sql_plain = ("SELECT session_id, count(*), max(timestamp), min(timestamp) "
                 "FROM messages GROUP BY session_id")

    def run():
        try:
            return conn.execute(sql_hinted).fetchall()
        except sqlite3.OperationalError:
            return conn.execute(sql_plain).fetchall()

    rows = timed("q:message_extents(group-by)", run, timings)
    return {r[0]: (int(r[1]), r[2], r[3]) for r in rows}


def compute_doomed(conn: sqlite3.Connection, cutoff: float,
                   timings: Optional[Dict[str, float]] = None,
                   stale_locks_ok: bool = False) -> DoomedResult:
    """The exact §b2 filter, with the lineage clause iterated to a fixed point.

    `stale_locks_ok` is OFF by default: the literal §b2 SQL protects a session that has
    ANY `session_turn_leases` / `compression_locks` row, expired or not.  Turning it on
    protects only rows with `expires_at > now`, which is what the Risk section means by
    "a live lease or lock".  It changes the doomed set materially on the live database
    (see DRYRUN.md) so it is an explicit operator decision, never a default.
    """
    timings = timings if timings is not None else {}
    now = time.time()

    sessions = load_sessions(conn, timings)
    extents = load_message_extents(conn, timings)

    def _live_only(rows):
        return {r[0] for r in rows if (not stale_locks_ok) or ((r[1] or 0.0) > now)}

    leases = timed(
        "q:session_turn_leases",
        lambda: _live_only(conn.execute(
            "SELECT conversation_id, expires_at FROM session_turn_leases").fetchall()),
        timings,
    )
    locks = timed(
        "q:compression_locks",
        lambda: _live_only(conn.execute(
            "SELECT session_id, expires_at FROM compression_locks").fetchall()),
        timings,
    )

    for s in sessions:
        c, mx, mn = extents.get(s.id, (0, None, None))
        s.msg_count, s.msg_max_ts, s.msg_min_ts = c, mx, mn

    excluded = {
        "pinned": 0,
        "turn_lease": 0,
        "compression_lock": 0,
        "effective_activity_ge_cutoff": 0,
        "has_message_ge_cutoff": 0,
    }

    t0 = time.time()
    candidates: set = set()
    for s in sessions:
        if s.pinned:
            excluded["pinned"] += 1
            continue
        if s.id in leases:
            excluded["turn_lease"] += 1
            continue
        if s.id in locks:
            excluded["compression_lock"] += 1
            continue
        # COALESCE(last_activity_at, max(message.timestamp), ended_at, started_at)
        eff = s.last_activity_at
        if eff is None:
            eff = s.msg_max_ts
        if eff is None:
            eff = s.ended_at
        if eff is None:
            eff = s.started_at
        if eff is None or eff >= cutoff:
            excluded["effective_activity_ge_cutoff"] += 1
            continue
        # NOT EXISTS (SELECT 1 FROM messages WHERE session_id=s.id AND timestamp>=cutoff)
        if s.msg_max_ts is not None and s.msg_max_ts >= cutoff:
            excluded["has_message_ge_cutoff"] += 1
            continue
        candidates.add(s.id)

    # Whole-lineage-or-nothing: drop any session that still has a child outside the set.
    # Iterated to a fixed point (MEASUREMENT "Risk" item 4).
    children: Dict[str, List[str]] = {}
    for s in sessions:
        if s.parent:
            children.setdefault(s.parent, []).append(s.id)

    rescued: List[str] = []
    iterations = 0
    while True:
        iterations += 1
        drop = [sid for sid in candidates
                if any(c not in candidates for c in children.get(sid, ()))]
        if not drop:
            break
        for sid in drop:
            candidates.discard(sid)
            rescued.append(sid)
    excluded["lineage_parent_of_live_child"] = len(rescued)
    timings["compute:filter+lineage"] = round(time.time() - t0, 3)

    all_ids = [s.id for s in sessions]
    doomed = sorted(candidates)
    kept = sorted(set(all_ids) - candidates)

    # Assertions from the Risk section — belt and braces, cheap.
    assert not (candidates & leases), "doomed set intersects session_turn_leases"
    assert not (candidates & locks), "doomed set intersects compression_locks"
    pinned_ids = {s.id for s in sessions if s.pinned}
    assert not (candidates & pinned_ids), "doomed set intersects pinned sessions"

    return DoomedResult(cutoff=cutoff, sessions=sessions, doomed_ids=doomed,
                        kept_ids=kept, excluded=excluded, lineage_rescued=sorted(rescued),
                        lineage_iterations=iterations, timings=timings)


# --------------------------------------------------------------------------------------
# byte accounting
# --------------------------------------------------------------------------------------


def _payload_expr() -> str:
    return " + ".join("COALESCE(octet_length(%s),0)" % c for c in PAYLOAD_COLUMNS)


def exact_bytes_by_set(conn: sqlite3.Connection, ids: Sequence[str], chunk: int = 100
                       ) -> Tuple[int, int]:
    """(rows, payload_bytes) for messages owned by `ids`, chunked so no read is long."""
    total_rows = 0
    total_bytes = 0
    expr = _payload_expr()
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        q = ("SELECT count(*), COALESCE(sum(%s),0) FROM messages WHERE session_id IN (%s)"
             % (expr, ",".join("?" * len(part))))
        r = conn.execute(q, part).fetchone()
        total_rows += int(r[0] or 0)
        total_bytes += int(r[1] or 0)
    return total_rows, total_bytes


def sampled_bytes(conn: sqlite3.Connection, doomed: set, sample_n: int = 20000,
                  chunk: int = 500, seed: int = 20260909) -> Dict[str, Any]:
    """Uniform random rowids in [1, max(id)] — the MEASUREMENT §2.3 method.

    Returns sampled sums split doomed/kept plus the derived extrapolation.  Explicitly
    labelled an ESTIMATE by the caller; never presented as a measurement.
    """
    max_id = conn.execute("SELECT COALESCE(max(id),0) FROM messages").fetchone()[0]
    total_rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    if not max_id or not total_rows:
        return {"mode": "sample", "sampled_ids": 0, "hits": 0, "doomed_bytes": 0,
                "kept_bytes": 0, "est_total_payload_bytes": 0,
                "est_doomed_payload_bytes": 0, "est_kept_payload_bytes": 0}
    rnd = random.Random(seed)
    ids = sorted({rnd.randint(1, max_id) for _ in range(sample_n)})
    expr = _payload_expr()
    hits = 0
    d_bytes = 0
    k_bytes = 0
    d_rows = 0
    k_rows = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        q = ("SELECT session_id, %s FROM messages WHERE id IN (%s)"
             % (expr, ",".join("?" * len(part))))
        for sid, nbytes in conn.execute(q, part).fetchall():
            hits += 1
            if sid in doomed:
                d_rows += 1
                d_bytes += int(nbytes or 0)
            else:
                k_rows += 1
                k_bytes += int(nbytes or 0)
    tot_bytes = d_bytes + k_bytes
    mean = (tot_bytes / hits) if hits else 0.0
    est_total = int(round(mean * total_rows))
    share = (d_bytes / tot_bytes) if tot_bytes else 0.0
    return {
        "mode": "sample",
        "sample_requested": sample_n,
        "sampled_ids": len(ids),
        "hits": hits,
        "hit_rate": round(hits / len(ids), 4) if ids else 0.0,
        "sampled_doomed_rows": d_rows,
        "sampled_kept_rows": k_rows,
        "sampled_doomed_bytes": d_bytes,
        "sampled_kept_bytes": k_bytes,
        "mean_payload_bytes_per_message": round(mean, 1),
        "doomed_payload_share": round(share, 6),
        "est_total_payload_bytes": est_total,
        "est_doomed_payload_bytes": int(round(est_total * share)),
        "est_kept_payload_bytes": int(round(est_total * (1.0 - share))),
        "note": "ESTIMATE by uniform rowid sampling (MEASUREMENT §2.3 method), not a scan",
    }


# --------------------------------------------------------------------------------------
# host probes  (never signal anything)
# --------------------------------------------------------------------------------------


def proc_cmdline(pid: str) -> str:
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def procs_holding_db(db_path: str) -> List[Dict[str, Any]]:
    """/proc/*/fd scan for holders of <db>, <db>-wal, <db>-shm.  Read-only, no signals."""
    target = os.path.abspath(db_path)
    prefixes = (target, target + "-wal", target + "-shm", target + "-journal")
    me = str(os.getpid())
    out: List[Dict[str, Any]] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        fddir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue
        hit = None
        for fd in fds:
            try:
                tgt = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if tgt in prefixes:
                hit = tgt
                break
        if hit:
            out.append({"pid": int(pid), "fd_target": hit, "cmdline": proc_cmdline(pid)})
    return out


def hermes_procs() -> List[Dict[str, Any]]:
    me = str(os.getpid())
    out: List[Dict[str, Any]] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        cl = proc_cmdline(pid)
        if not cl:
            continue
        if os.path.abspath(__file__) in cl or "hermes_state_archive" in cl:
            continue  # never self-match
        for pat in HERMES_PROC_PATTERNS:
            if pat in cl:
                out.append({"pid": int(pid), "pattern": pat, "cmdline": cl})
                break
    return out


def free_bytes(path: str) -> int:
    st = os.statvfs(os.path.dirname(os.path.abspath(path)) or "/")
    return st.f_bavail * st.f_frsize


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def db_file_sizes(db: str) -> Dict[str, int]:
    return {
        "db": file_size(db),
        "wal": file_size(db + "-wal"),
        "shm": file_size(db + "-shm"),
    }


def db_space(conn: sqlite3.Connection) -> Dict[str, int]:
    pc = conn.execute("PRAGMA page_count").fetchone()[0]
    fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
    ps = conn.execute("PRAGMA page_size").fetchone()[0]
    return {
        "page_count": int(pc),
        "freelist_count": int(fl),
        "page_size": int(ps),
        "file_bytes_from_pages": int(pc) * int(ps),
        "content_bytes": (int(pc) - int(fl)) * int(ps),
    }


def read_vacuum_after_prune(config_path: str) -> Tuple[Optional[bool], str]:
    """Return (value, how).  None when the key is absent or unreadable."""
    if not os.path.exists(config_path):
        return None, "config file not found: %s" % config_path
    try:
        import yaml  # type: ignore
        with open(config_path) as fh:
            data = yaml.safe_load(fh) or {}
        sessions = data.get("sessions") or {}
        if "vacuum_after_prune" in sessions:
            return bool(sessions["vacuum_after_prune"]), "yaml"
        return None, "yaml: sessions.vacuum_after_prune absent"
    except Exception as exc:  # noqa: BLE001 - fall back to a textual scan
        try:
            with open(config_path) as fh:
                text = fh.read()
        except OSError as exc2:
            return None, "unreadable: %s" % exc2
        m = re.search(r"^sessions:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
        block = m.group(1) if m else text
        m2 = re.search(r"^\s*vacuum_after_prune:\s*(\S+)", block, re.M)
        if m2:
            return m2.group(1).strip().lower() in ("true", "yes", "on", "1"), \
                "regex (yaml unavailable: %s)" % exc
        return None, "regex: key absent (yaml unavailable: %s)" % exc


# --------------------------------------------------------------------------------------
# PLAN.json
# --------------------------------------------------------------------------------------


def plan_path(args) -> str:
    return args.plan or os.path.join(args.receipts, "PLAN.json")


def load_plan(args) -> Dict[str, Any]:
    p = plan_path(args)
    if not os.path.exists(p):
        die("no PLAN.json at %s — run `archive.py plan` first" % p)
    with open(p) as fh:
        plan = json.load(fh)
    if plan.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        die("PLAN.json schema version %r != %r" % (plan.get("plan_schema_version"),
                                                   PLAN_SCHEMA_VERSION))
    if os.path.abspath(plan["db"]) != os.path.abspath(args.db):
        die("PLAN.json was built for %s but --db is %s" % (plan["db"], args.db))
    if not plan.get("doomed_session_ids"):
        die("PLAN.json has an empty doomed set — nothing to do")
    return plan


def assert_schema_unchanged(conn: sqlite3.Connection, plan: Dict[str, Any]) -> None:
    fp = schema_fingerprint(conn)
    if fp != plan.get("schema_fingerprint"):
        die("live schema fingerprint %s != PLAN.json %s — re-run `plan`"
            % (fp[:16], str(plan.get("schema_fingerprint"))[:16]))


# --------------------------------------------------------------------------------------
# subcommand: plan
# --------------------------------------------------------------------------------------


def cmd_plan(args) -> int:
    conn = connect_ro(args.db)
    timings: Dict[str, float] = {}
    try:
        started = time.time()
        cutoff = args.cutoff if args.cutoff is not None else (time.time() - args.days * 86400.0)
        res = compute_doomed(conn, cutoff, timings, stale_locks_ok=args.stale_locks_ok)
        doomed_set = set(res.doomed_ids)

        by_id = {s.id: s for s in res.sessions}
        doomed_msgs = sum(by_id[i].msg_count for i in res.doomed_ids)
        kept_msgs = sum(by_id[i].msg_count for i in res.kept_ids)

        total_msgs = timed(
            "q:count(messages)",
            lambda: conn.execute("SELECT count(*) FROM messages").fetchone()[0],
            timings,
        )
        orphan_msgs = total_msgs - doomed_msgs - kept_msgs

        smu = timed(
            "q:session_model_usage",
            lambda: conn.execute("SELECT count(*) FROM session_model_usage").fetchone()[0],
            timings,
        )

        mode = args.bytes_mode
        if mode == "auto":
            mode = "exact" if total_msgs <= 200000 else "sample"
        if mode == "exact":
            t0 = time.time()
            d_rows, d_bytes = exact_bytes_by_set(conn, res.doomed_ids, args.id_chunk)
            k_rows, k_bytes = exact_bytes_by_set(conn, res.kept_ids, args.id_chunk)
            timings["q:bytes(exact,chunked)"] = round(time.time() - t0, 3)
            log("%-42s %8.3f s" % ("q:bytes(exact,chunked)",
                                   timings["q:bytes(exact,chunked)"]))
            bytes_block = {
                "mode": "exact",
                "doomed_rows": d_rows, "doomed_payload_bytes": d_bytes,
                "kept_rows": k_rows, "kept_payload_bytes": k_bytes,
                "est_doomed_payload_bytes": d_bytes,
                "est_kept_payload_bytes": k_bytes,
                "est_total_payload_bytes": d_bytes + k_bytes,
                "doomed_payload_share": round(d_bytes / (d_bytes + k_bytes), 6)
                if (d_bytes + k_bytes) else 0.0,
            }
        elif mode == "sample":
            t0 = time.time()
            bytes_block = sampled_bytes(conn, doomed_set, args.sample_n)
            timings["q:bytes(sampled)"] = round(time.time() - t0, 3)
            log("%-42s %8.3f s" % ("q:bytes(sampled)", timings["q:bytes(sampled)"]))
        else:
            bytes_block = {"mode": "none"}

        space = timed("q:pragmas", lambda: db_space(conn), timings)
        fp = schema_fingerprint(conn)
        ddl = {t: object_sql(conn, t) for t in ARCHIVE_TABLES}
        missing = [t for t, s in ddl.items() if not s]
        if missing:
            die("missing table DDL in sqlite_master for: %s" % ", ".join(missing))

        pinned = sum(1 for s in res.sessions if s.pinned)
        ended_null = sum(1 for s in res.sessions if s.ended_at is None)

        plan = {
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "tool_version": TOOL_VERSION,
            "created_at": iso(started),
            "db": os.path.abspath(args.db),
            "db_sizes_at_plan": db_file_sizes(args.db),
            "db_space_at_plan": space,
            "schema_fingerprint": fp,
            "table_ddl": ddl,
            "cutoff_epoch": cutoff,
            "cutoff_iso": iso(cutoff),
            "cutoff_days": args.days if args.cutoff is None else None,
            "stale_locks_ok": bool(args.stale_locks_ok),
            "counts": {
                "sessions_total": len(res.sessions),
                "sessions_to_move": len(res.doomed_ids),
                "sessions_to_keep": len(res.kept_ids),
                "sessions_pinned": pinned,
                "sessions_ended_at_null": ended_null,
                "messages_total": total_msgs,
                "messages_to_move": doomed_msgs,
                "messages_to_keep": kept_msgs,
                "messages_orphan_no_session_row": orphan_msgs,
                "session_model_usage_total": smu,
            },
            "exclusions": res.excluded,
            "lineage": {
                "iterations_to_fixed_point": res.lineage_iterations,
                "rescued_parent_count": len(res.lineage_rescued),
                "rescued_parent_ids": res.lineage_rescued[:200],
            },
            "bytes": bytes_block,
            "doomed_session_ids": res.doomed_ids,
            "doomed_session_message_counts": {i: by_id[i].msg_count for i in res.doomed_ids},
            "query_timings_s": timings,
            "read_only": True,
        }
        out = plan_path(args)
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(plan, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, out)

        c = plan["counts"]
        print("=" * 78)
        print("PLAN  (read-only)  db=%s" % plan["db"])
        print("cutoff  %s  (%.0f, %s days)" % (plan["cutoff_iso"], cutoff,
                                               args.days if args.cutoff is None else -1))
        print("-" * 78)
        print("sessions   total %8d   MOVE %8d   KEEP %8d   (pinned kept %d)"
              % (c["sessions_total"], c["sessions_to_move"], c["sessions_to_keep"], pinned))
        print("messages   total %8d   MOVE %8d   KEEP %8d   (orphan %d)"
              % (c["messages_total"], c["messages_to_move"], c["messages_to_keep"],
                 c["messages_orphan_no_session_row"]))
        if bytes_block.get("mode") == "exact":
            print("bytes      MOVE %s   KEEP %s   [exact, chunked]"
                  % (_h(bytes_block["doomed_payload_bytes"]),
                     _h(bytes_block["kept_payload_bytes"])))
        elif bytes_block.get("mode") == "sample":
            print("bytes      MOVE ~%s  KEEP ~%s  [ESTIMATE: %d sampled rows, share %.4f]"
                  % (_h(bytes_block["est_doomed_payload_bytes"]),
                     _h(bytes_block["est_kept_payload_bytes"]),
                     bytes_block["hits"], bytes_block["doomed_payload_share"]))
        print("-" * 78)
        print("excluded from the doomed set:")
        for k, v in sorted(res.excluded.items()):
            print("   %-34s %8d" % (k, v))
        print("lineage fixed point reached in %d iteration(s); %d parent(s) rescued"
              % (res.lineage_iterations, len(res.lineage_rescued)))
        print("-" * 78)
        for k, v in timings.items():
            print("   %-34s %8.3f s" % (k, v))
        print("wall total %.3f s" % (time.time() - started))
        print("PLAN.json -> %s" % out)
        print("=" * 78)
        return 0
    finally:
        conn.close()


def _h(n: int) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0
    return "%.2f B" % n


# --------------------------------------------------------------------------------------
# subcommand: preflight   (MEASUREMENT §7b5)
# --------------------------------------------------------------------------------------


def cmd_preflight(args) -> int:
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%-4s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))

    conn = connect_ro(args.db)
    try:
        now = time.time()
        # b5.1 — leases and locks
        lease_rows = conn.execute(
            "SELECT conversation_id, holder, expires_at FROM session_turn_leases").fetchall()
        live_leases = [r for r in lease_rows if (r[2] or 0) > now]
        add("session_turn_leases_live", len(live_leases) == 0,
            "%d live / %d total rows" % (len(live_leases), len(lease_rows)))

        lock_rows = conn.execute(
            "SELECT session_id, holder, expires_at FROM compression_locks").fetchall()
        live_locks = [r for r in lock_rows if (r[2] or 0) > now]
        add("compression_locks_live", len(live_locks) == 0,
            "%d live / %d total rows" % (len(live_locks), len(lock_rows)))

        space = db_space(conn)
    finally:
        conn.close()

    # b5.4 — /proc fd scan
    holders = procs_holding_db(args.db)
    add("no_process_holds_state_db", len(holders) == 0,
        "%d holder(s): %s" % (len(holders),
                              "; ".join("pid %d %s" % (h["pid"], h["cmdline"][:70])
                                        for h in holders) or "none"))

    # b5.2/b5.3 — gateway + any hermes CLI (each CLI start runs auto-maintenance)
    procs = hermes_procs()
    gw = [p for p in procs if GATEWAY_PROC_PATTERN in p["cmdline"]]
    add("gateway_not_running", len(gw) == 0,
        "%d: %s" % (len(gw), "; ".join("pid %d" % p["pid"] for p in gw) or "none"))
    add("no_hermes_processes", len(procs) == 0,
        "%d: %s" % (len(procs),
                    "; ".join("pid %d %s" % (p["pid"], p["pattern"]) for p in procs) or "none"))

    # b5.5 — free disk for the copy phase, corrected 2x rule
    free = free_bytes(args.db)
    plan_file = plan_path(args)
    if os.path.exists(plan_file):
        with open(plan_file) as fh:
            plan = json.load(fh)
        moved = int(plan.get("bytes", {}).get("est_doomed_payload_bytes") or 0)
        basis = "PLAN.json est_doomed_payload_bytes"
    else:
        moved = space["content_bytes"]
        basis = "no PLAN.json — worst case: whole live content"
    need_copy = 2 * moved + TWO_GIB
    add("free_disk_for_copy", free >= need_copy,
        "free %s, need 2x%s + 2 GiB = %s  [%s]"
        % (_h(free), _h(moved), _h(need_copy), basis))

    # Informational: the VACUUM rule (MEASUREMENT §7c defect c2 — the prune script's
    # `size + 2 GiB` is wrong by ~2x).
    need_vac = 2 * space["content_bytes"] + TWO_GIB
    print("%-4s %-34s free %s, VACUUM would need 2x%s + 2 GiB = %s"
          % ("INFO", "vacuum_headroom (advisory)", _h(free),
             _h(space["content_bytes"]), _h(need_vac)))
    checks.append({"check": "vacuum_headroom_advisory", "ok": free >= need_vac,
                   "advisory": True,
                   "detail": {"free": free, "need": need_vac,
                              "content_bytes": space["content_bytes"]}})

    # b5 / Rank 0 — the armed VACUUM must be disarmed
    vap, how = read_vacuum_after_prune(args.config)
    add("sessions.vacuum_after_prune_is_false", vap is False,
        "value=%r via %s (%s)" % (vap, how, args.config))

    hard = [c for c in checks if not c.get("advisory") and not c["ok"]]
    payload = {
        "db": os.path.abspath(args.db),
        "db_sizes": db_file_sizes(args.db),
        "db_space": space,
        "free_bytes": free,
        "checks": checks,
        "failed": [c["check"] for c in hard],
        "result": "PASS" if not hard else "FAIL",
    }
    write_receipt(args.receipts, "preflight", payload)
    print("-" * 78)
    if hard:
        print("PREFLIGHT FAIL: %s" % ", ".join(c["check"] for c in hard))
        return 1
    print("PREFLIGHT PASS — all §b5 preconditions hold")
    return 0


# --------------------------------------------------------------------------------------
# subcommand: backup   (MEASUREMENT §7b5.5)
# --------------------------------------------------------------------------------------


def cmd_backup(args) -> int:
    sizes = db_file_sizes(args.db)
    total = sum(sizes.values())
    dest_dir = args.backup_dir or (os.path.dirname(os.path.abspath(args.db)) + "/backups")
    os.makedirs(dest_dir, exist_ok=True)
    free = free_bytes(dest_dir + "/x")
    if free < total + 512 * 1024 * 1024:
        die("not enough free space for the backup: need %s + 512 MiB, free %s"
            % (_h(total), _h(free)))

    # Never copy the .db alone: live frames sit only in the WAL (MEASUREMENT §5.1).
    holders = procs_holding_db(args.db)
    if holders and not args.allow_writers:
        die("%d process(es) still hold state.db; a plain three-file copy is only "
            "consistent with zero writers. Holders: %s"
            % (len(holders), "; ".join("pid %d" % h["pid"] for h in holders)))

    stamp = now_stamp()
    out: Dict[str, Any] = {"backup_dir": dest_dir, "stamp": stamp, "files": {}}
    for suffix in ("", "-wal", "-shm"):
        src = args.db + suffix
        if not os.path.exists(src):
            out["files"][suffix or ".db"] = {"present": False}
            continue
        dst = os.path.join(dest_dir, "%s.%s%s" % (os.path.basename(args.db), stamp, suffix))
        t0 = time.time()
        shutil.copyfile(src, dst)
        dt = time.time() - t0
        digest = sha256_file(dst)
        out["files"][suffix or ".db"] = {
            "present": True, "src": src, "dst": dst,
            "bytes": file_size(dst), "sha256": digest, "copy_seconds": round(dt, 3),
        }
        log("backed up %s -> %s (%s, %.1f s)" % (src, dst, _h(file_size(dst)), dt))

    out["db"] = os.path.abspath(args.db)
    out["sizes_at_backup"] = sizes
    out["result"] = "OK"
    write_receipt(args.receipts, "backup", out)
    print("BACKUP OK -> %s (%s total)" % (dest_dir, _h(total)))
    return 0


# --------------------------------------------------------------------------------------
# subcommand: copy   (MEASUREMENT §7b1 / §7b2)
# --------------------------------------------------------------------------------------


def archive_path(args, plan: Dict[str, Any]) -> str:
    if args.archive:
        return args.archive
    day = plan["created_at"][:10].replace("-", "")
    return os.path.join(os.path.dirname(os.path.abspath(args.db)),
                        "state-archive-%s.db" % day)


def _populate_doomed_temp(conn: sqlite3.Connection, ids: Sequence[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.doomed")
    conn.execute("CREATE TEMP TABLE doomed(id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO temp.doomed(id) VALUES (?)",
                     ((i,) for i in ids))


def cmd_copy(args) -> int:
    plan = load_plan(args)
    ids = plan["doomed_session_ids"]
    arch = archive_path(args, plan)

    conn = connect_rw(args.db, args.quiet_window)
    try:
        assert_schema_unchanged(conn, plan)

        # Re-assert the Risk-section invariants against the *live* tables right now.
        _populate_doomed_temp(conn, ids)
        # If the plan honoured every lease/lock row, so must this re-assertion; if the
        # plan was built with --stale-locks-ok, only LIVE rows may block the copy.
        lock_pred = "" if not plan.get("stale_locks_ok") else " AND expires_at > %f" % time.time()
        bad_lease = conn.execute(
            "SELECT count(*) FROM temp.doomed WHERE id IN "
            "(SELECT conversation_id FROM session_turn_leases WHERE 1=1%s)" % lock_pred
        ).fetchone()[0]
        bad_lock = conn.execute(
            "SELECT count(*) FROM temp.doomed WHERE id IN "
            "(SELECT session_id FROM compression_locks WHERE 1=1%s)" % lock_pred
        ).fetchone()[0]
        bad_pin = conn.execute(
            "SELECT count(*) FROM sessions WHERE COALESCE(pinned,0)=1 "
            "AND id IN (SELECT id FROM temp.doomed)").fetchone()[0]
        bad_child = conn.execute(
            "SELECT count(*) FROM sessions c WHERE c.parent_session_id IN "
            "(SELECT id FROM temp.doomed) AND c.id NOT IN (SELECT id FROM temp.doomed)"
        ).fetchone()[0]
        if bad_lease or bad_lock or bad_pin or bad_child:
            die("ABORT — doomed set is no longer safe: leases=%d locks=%d pinned=%d "
                "live_children_of_doomed_parent=%d" % (bad_lease, bad_lock, bad_pin,
                                                       bad_child))
        log("safety re-assertion OK (leases/locks/pinned/lineage all 0)")

        if os.path.exists(arch) and not args.resume:
            die("archive already exists: %s (pass --resume to add to it)" % arch)
        conn.execute("ATTACH DATABASE ? AS arch", (arch,))
        if args.fast_archive:
            # Bulk-load into a brand new file: no rollback journal, no fsync.
            # Only sound when the archive can simply be deleted and rebuilt on failure.
            conn.execute("PRAGMA arch.journal_mode=OFF")
            conn.execute("PRAGMA arch.synchronous=OFF")

        created = []
        for t in ARCHIVE_TABLES:
            exists = conn.execute(
                "SELECT count(*) FROM arch.sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()[0]
            if not exists:
                ddl = plan["table_ddl"][t]
                conn.execute(rewrite_create_table(ddl, t, "arch"))
                created.append(t)
        log("archive tables created: %s" % (", ".join(created) or "(all pre-existing)"))

        # One transaction for the whole copy (MEASUREMENT §7b2).
        # INSERT OR IGNORE so an interrupted copy is safely resumable.
        t0 = time.time()
        conn.execute("BEGIN IMMEDIATE")
        n_sess = conn.execute(
            "INSERT OR IGNORE INTO arch.sessions "
            "SELECT * FROM main.sessions WHERE id IN (SELECT id FROM temp.doomed)").rowcount
        n_msg = conn.execute(
            "INSERT OR IGNORE INTO arch.messages "
            "SELECT * FROM main.messages WHERE session_id IN (SELECT id FROM temp.doomed)"
        ).rowcount
        n_smu = conn.execute(
            "INSERT OR IGNORE INTO arch.session_model_usage "
            "SELECT * FROM main.session_model_usage "
            "WHERE session_id IN (SELECT id FROM temp.doomed)").rowcount
        conn.execute("COMMIT")
        dt = time.time() - t0
        log("copied sessions=%d messages=%d session_model_usage=%d in %.1f s"
            % (n_sess, n_msg, n_smu, dt))

        if args.archive_index:
            conn.execute("CREATE INDEX IF NOT EXISTS arch.idx_arch_messages_session "
                         "ON messages(session_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS arch.idx_arch_sessions_parent "
                         "ON sessions(parent_session_id)")
            log("archive lookup indexes created (for /resume recovery)")

        arch_counts = {
            t: conn.execute("SELECT count(*) FROM arch.%s" % t).fetchone()[0]
            for t in ARCHIVE_TABLES
        }
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.execute("DETACH DATABASE arch")
    finally:
        conn.close()

    payload = {
        "db": os.path.abspath(args.db),
        "archive": os.path.abspath(arch),
        "archive_bytes": file_size(arch),
        "plan": plan_path(args),
        "cutoff_iso": plan["cutoff_iso"],
        "inserted": {"sessions": n_sess, "messages": n_msg,
                     "session_model_usage": n_smu},
        "archive_row_counts": arch_counts,
        "expected_from_plan": {
            "sessions": plan["counts"]["sessions_to_move"],
            "messages": plan["counts"]["messages_to_move"],
        },
        "copy_seconds": round(dt, 3),
        "created_tables": created,
        "result": "OK",
    }
    write_receipt(args.receipts, "copy", payload)
    print("COPY OK -> %s (%s)" % (arch, _h(file_size(arch))))
    print("  sessions %d  messages %d  session_model_usage %d"
          % (arch_counts["sessions"], arch_counts["messages"],
             arch_counts["session_model_usage"]))
    return 0


# --------------------------------------------------------------------------------------
# subcommand: verify   (MEASUREMENT §7b3)
# --------------------------------------------------------------------------------------


def cmd_verify(args) -> int:
    plan = load_plan(args)
    ids = plan["doomed_session_ids"]
    arch = archive_path(args, plan)
    if not os.path.exists(arch):
        die("archive not found: %s" % arch)

    live = connect_ro(args.db)
    a = connect_ro(arch)
    problems: List[str] = []
    try:
        t0 = time.time()
        live_msgs, live_bytes = exact_bytes_by_set(live, ids, args.id_chunk)
        live_sess = _count_in(live, "sessions", "id", ids, args.id_chunk)
        live_smu = _count_in(live, "session_model_usage", "session_id", ids, args.id_chunk)
        t_live = time.time() - t0

        t0 = time.time()
        arch_msgs, arch_bytes = exact_bytes_by_set(a, ids, args.id_chunk)
        arch_sess = _count_in(a, "sessions", "id", ids, args.id_chunk)
        arch_smu = _count_in(a, "session_model_usage", "session_id", ids, args.id_chunk)
        arch_total_msgs = a.execute("SELECT count(*) FROM messages").fetchone()[0]
        t_arch = time.time() - t0

        expected_msgs = plan["counts"]["messages_to_move"]
        expected_sess = plan["counts"]["sessions_to_move"]

        if live_msgs == 0 and live_sess == 0:
            phase = "post-delete"
        elif live_msgs == arch_msgs and live_sess == arch_sess:
            phase = "pre-delete"
        else:
            phase = "mismatch"

        print("phase detected: %s" % phase)
        print("  live  sessions %8d  messages %8d  smu %6d  bytes %s   (%.1f s)"
              % (live_sess, live_msgs, live_smu, _h(live_bytes), t_live))
        print("  arch  sessions %8d  messages %8d  smu %6d  bytes %s   (%.1f s)"
              % (arch_sess, arch_msgs, arch_smu, _h(arch_bytes), t_arch))
        print("  plan  sessions %8d  messages %8d" % (expected_sess, expected_msgs))

        if arch_msgs < expected_msgs:
            problems.append("archive holds %d messages, plan expected >= %d"
                            % (arch_msgs, expected_msgs))
        if arch_sess < expected_sess:
            problems.append("archive holds %d sessions, plan expected >= %d"
                            % (arch_sess, expected_sess))
        if arch_total_msgs != arch_msgs:
            problems.append("archive holds %d messages not owned by the doomed set"
                            % (arch_total_msgs - arch_msgs))
        if phase == "pre-delete":
            if live_bytes != arch_bytes:
                problems.append("payload bytes differ: live %d vs arch %d"
                                % (live_bytes, arch_bytes))
            if live_smu != arch_smu:
                problems.append("session_model_usage differ: live %d vs arch %d"
                                % (live_smu, arch_smu))
        elif phase == "mismatch":
            problems.append("live/arch counts inconsistent with either phase "
                            "(live msgs %d, arch msgs %d)" % (live_msgs, arch_msgs))

        t0 = time.time()
        pragma = "quick_check" if args.quick else "integrity_check"
        ic = a.execute("PRAGMA %s" % pragma).fetchall()
        t_ic = time.time() - t0
        ic_ok = (len(ic) == 1 and ic[0][0] == "ok")
        print("  archive PRAGMA %s: %s (%.1f s)"
              % (pragma, "ok" if ic_ok else ic[:3], t_ic))
        if not ic_ok:
            problems.append("archive %s failed: %r" % (pragma, ic[:5]))
    finally:
        live.close()
        a.close()

    digest = None
    if not args.no_sha256:
        t0 = time.time()
        digest = sha256_file(arch)
        print("  archive sha256 %s (%.1f s)" % (digest, time.time() - t0))

    payload = {
        "db": os.path.abspath(args.db),
        "archive": os.path.abspath(arch),
        "archive_bytes": file_size(arch),
        "archive_sha256": digest,
        "phase": phase,
        "live": {"sessions": live_sess, "messages": live_msgs,
                 "session_model_usage": live_smu, "payload_bytes": live_bytes},
        "arch": {"sessions": arch_sess, "messages": arch_msgs,
                 "session_model_usage": arch_smu, "payload_bytes": arch_bytes},
        "expected_from_plan": {"sessions": expected_sess, "messages": expected_msgs},
        "integrity_check": "ok" if ic_ok else str(ic[:5]),
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    write_receipt(args.receipts, "verify", payload)
    if problems:
        print("-" * 78)
        for p in problems:
            print("PROBLEM: %s" % p)
        print("VERIFY FAIL")
        return 1
    print("VERIFY PASS")
    return 0


def _count_in(conn: sqlite3.Connection, table: str, col: str, ids: Sequence[str],
              chunk: int = 100) -> int:
    n = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        n += conn.execute(
            "SELECT count(*) FROM %s WHERE %s IN (%s)"
            % (table, col, ",".join("?" * len(part))), part).fetchone()[0]
    return n


# --------------------------------------------------------------------------------------
# subcommand: delete   (MEASUREMENT §7b4)
# --------------------------------------------------------------------------------------


def cmd_delete(args) -> int:
    plan = load_plan(args)
    ids = plan["doomed_session_ids"]

    if not args.skip_verify_gate:
        rec = latest_receipt(args.receipts, "verify")
        if not rec or rec.get("result") != "PASS":
            die("no PASSing verify receipt in %s — verify the archive before deleting "
                "(or pass --skip-verify-gate if you have verified out of band)"
                % args.receipts)
        if os.path.abspath(rec.get("db", "")) != os.path.abspath(args.db):
            die("latest verify receipt is for a different db: %r" % rec.get("db"))

    progress_log = os.path.join(args.receipts, "delete-progress.jsonl")
    os.makedirs(args.receipts, exist_ok=True)

    conn = connect_rw(args.db, args.quiet_window)
    started = time.time()
    batches = 0
    deleted_msgs = 0
    deleted_sess = 0
    deleted_smu = 0
    try:
        assert_schema_unchanged(conn, plan)
        _populate_doomed_temp(conn, ids)

        remaining = conn.execute(
            "SELECT count(*) FROM messages WHERE session_id IN (SELECT id FROM temp.doomed)"
        ).fetchone()[0]
        log("remaining doomed messages at start: %d (pinned PLAN set of %d sessions)"
            % (remaining, len(ids)))
        if remaining == 0:
            log("no doomed messages remain — message phase already complete")

        with open(progress_log, "a") as plog:
            plog.write(json.dumps({"event": "start", "at": iso(started),
                                   "remaining_messages": remaining,
                                   "plan": plan_path(args)}) + "\n")
            plog.flush()

            while True:
                if args.max_batches and batches >= args.max_batches:
                    log("--max-batches %d reached; stopping (resumable)" % args.max_batches)
                    break
                t0 = time.time()
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "  SELECT id FROM messages "
                    "  WHERE session_id IN (SELECT id FROM temp.doomed) LIMIT ?)",
                    (args.batch_size,))
                n = cur.rowcount
                conn.execute("COMMIT")
                dt = time.time() - t0
                if n <= 0:
                    break
                batches += 1
                deleted_msgs += n
                time.sleep(args.sleep_ms / 1000.0)
                ck = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if batches % args.log_every == 0 or n < args.batch_size:
                    rate = deleted_msgs / max(time.time() - started, 1e-6)
                    eta = (remaining - deleted_msgs) / rate if rate > 0 else -1
                    log("batch %6d  deleted %9d/%d  %.1f rows/s  eta %.0f min  "
                        "wal=%s" % (batches, deleted_msgs, remaining, rate, eta / 60.0,
                                    _h(file_size(args.db + "-wal"))))
                    plog.write(json.dumps({
                        "event": "batch", "at": iso(time.time()), "batch": batches,
                        "rows": n, "deleted_total": deleted_msgs,
                        "remaining_est": remaining - deleted_msgs,
                        "batch_seconds": round(dt, 3),
                        "wal_bytes": file_size(args.db + "-wal"),
                        "checkpoint": list(ck) if ck else None}) + "\n")
                    plog.flush()

            msgs_left = conn.execute(
                "SELECT count(*) FROM messages WHERE session_id IN "
                "(SELECT id FROM temp.doomed)").fetchone()[0]

            if msgs_left == 0 and not (args.max_batches and batches >= args.max_batches):
                # session_model_usage first (FK child), then the session rows themselves.
                # MEASUREMENT §7c defect c4: the prune script never deleted the session
                # rows, leaving 384 message-less sessions.
                conn.execute("BEGIN IMMEDIATE")
                deleted_smu = conn.execute(
                    "DELETE FROM session_model_usage WHERE session_id IN "
                    "(SELECT id FROM temp.doomed)").rowcount
                conn.execute("COMMIT")
                sess_ids = list(ids)
                for i in range(0, len(sess_ids), args.session_batch):
                    part = sess_ids[i:i + args.session_batch]
                    conn.execute("BEGIN IMMEDIATE")
                    # break the self-FK before removing parents
                    conn.execute(
                        "UPDATE sessions SET parent_session_id=NULL "
                        "WHERE parent_session_id IN (%s)" % ",".join("?" * len(part)), part)
                    deleted_sess += conn.execute(
                        "DELETE FROM sessions WHERE id IN (%s)"
                        % ",".join("?" * len(part)), part).rowcount
                    conn.execute("COMMIT")
                    time.sleep(args.sleep_ms / 1000.0)
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                log("deleted sessions=%d session_model_usage=%d" % (deleted_sess, deleted_smu))

                if args.sweep_orphans:
                    conn.execute("BEGIN IMMEDIATE")
                    n = conn.execute(
                        "DELETE FROM session_model_usage WHERE session_id NOT IN "
                        "(SELECT id FROM sessions)").rowcount
                    conn.execute("COMMIT")
                    deleted_smu += n
                    log("orphan sweep removed %d session_model_usage rows" % n)

            sess_left = _count_in(conn, "sessions", "id", ids, args.id_chunk)
            plog.write(json.dumps({"event": "end", "at": iso(time.time()),
                                   "messages_left": msgs_left,
                                   "sessions_left": sess_left,
                                   "deleted_messages": deleted_msgs,
                                   "deleted_sessions": deleted_sess}) + "\n")
            plog.flush()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        conn.close()

    elapsed = time.time() - started
    complete = (msgs_left == 0 and sess_left == 0)
    payload = {
        "db": os.path.abspath(args.db),
        "plan": plan_path(args),
        "cutoff_iso": plan["cutoff_iso"],
        "batch_size": args.batch_size,
        "sleep_ms": args.sleep_ms,
        "batches": batches,
        "deleted": {"messages": deleted_msgs, "sessions": deleted_sess,
                    "session_model_usage": deleted_smu},
        "remaining": {"messages": msgs_left, "sessions": sess_left},
        "elapsed_seconds": round(elapsed, 1),
        "rows_per_second": round(deleted_msgs / elapsed, 1) if elapsed > 0 else None,
        "db_sizes_after": db_file_sizes(args.db),
        "complete": complete,
        "result": "OK" if complete else "PARTIAL",
        "progress_log": progress_log,
    }
    write_receipt(args.receipts, "delete", payload)
    print("DELETE %s: %d messages, %d sessions, %d smu in %.1f s (%d batches)"
          % ("COMPLETE" if complete else "PARTIAL (resumable — re-run)",
             deleted_msgs, deleted_sess, deleted_smu, elapsed, batches))
    if deleted_msgs == 0 and deleted_sess == 0 and complete:
        print("  (no-op: the pinned doomed set is already gone)")
    return 0 if complete else 3


# --------------------------------------------------------------------------------------
# FTS subcommands
# --------------------------------------------------------------------------------------


def _assert_sole_writer(conn: sqlite3.Connection, db: str, allow: bool) -> None:
    if allow:
        log("WARNING: --allow-other-writers given; skipping the sole-writer assertion")
        return
    holders = [h for h in procs_holding_db(db)]
    if holders:
        die("refusing: %d other process(es) hold %s — a dropped trigger silently "
            "desynchronises the FTS index for anyone else's insert. Holders: %s"
            % (len(holders), db, "; ".join("pid %d" % h["pid"] for h in holders)))
    now = time.time()
    n = conn.execute("SELECT count(*) FROM session_turn_leases WHERE expires_at > ?",
                     (now,)).fetchone()[0]
    m = conn.execute("SELECT count(*) FROM compression_locks WHERE expires_at > ?",
                     (now,)).fetchone()[0]
    if n or m:
        die("refusing: %d live turn lease(s), %d live compression lock(s)" % (n, m))


def cmd_rebuild_fts(args) -> int:
    conn = connect_rw(args.db, args.quiet_window)
    results = {}
    try:
        _assert_sole_writer(conn, args.db, args.allow_other_writers)
        targets = args.index or list(FTS_INDEXES)
        for name in targets:
            present = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            if not present:
                results[name] = {"present": False}
                log("FTS index %s absent — skipped" % name)
                continue
            t0 = time.time()
            conn.execute("INSERT INTO %s(%s) VALUES('rebuild')" % (name, name))
            dt = time.time() - t0
            results[name] = {"present": True, "seconds": round(dt, 3)}
            log("rebuilt %s in %.1f s" % (name, dt))
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        conn.close()
    payload = {"db": os.path.abspath(args.db), "rebuilt": results,
               "db_sizes_after": db_file_sizes(args.db), "result": "OK"}
    write_receipt(args.receipts, "rebuild-fts", payload)
    print("REBUILD-FTS OK: %s" % json.dumps(results))
    return 0


def cmd_drop_fts_triggers(args) -> int:
    """Fast delete path: drop the six FTS triggers, delete, rebuild, restore.

    ONLY safe with zero other writers — a dropped trigger silently desynchronises both
    indexes for anyone else's INSERT (MEASUREMENT §7b4 note).
    """
    conn = connect_rw(args.db, args.quiet_window)
    saved = {}
    try:
        _assert_sole_writer(conn, args.db, args.allow_other_writers)
        for t in FTS_TRIGGERS:
            sql = object_sql(conn, t)
            if sql:
                saved[t] = sql
                conn.execute("DROP TRIGGER %s" % t)
                log("dropped trigger %s" % t)
    finally:
        conn.close()
    path = os.path.join(args.receipts, "fts-triggers.json")
    os.makedirs(args.receipts, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"db": os.path.abspath(args.db), "saved_at": iso(time.time()),
                   "triggers": saved}, fh, indent=2)
    write_receipt(args.receipts, "drop-fts-triggers",
                  {"db": os.path.abspath(args.db), "dropped": sorted(saved),
                   "saved_to": path, "result": "OK"})
    print("DROPPED %d FTS trigger(s); DDL saved to %s" % (len(saved), path))
    print("The FTS indexes are now STALE. You MUST run `rebuild-fts` before any reader "
          "trusts search, and `restore-fts-triggers` before any other writer starts.")
    return 0


def cmd_restore_fts_triggers(args) -> int:
    path = args.triggers_file or os.path.join(args.receipts, "fts-triggers.json")
    if not os.path.exists(path):
        die("no saved trigger DDL at %s" % path)
    with open(path) as fh:
        saved = json.load(fh)
    if os.path.abspath(saved.get("db", "")) != os.path.abspath(args.db):
        die("saved trigger DDL is for %r, not %s" % (saved.get("db"), args.db))
    conn = connect_rw(args.db, args.quiet_window)
    restored = []
    try:
        _assert_sole_writer(conn, args.db, args.allow_other_writers)
        for name, sql in sorted(saved["triggers"].items()):
            exists = conn.execute("SELECT count(*) FROM sqlite_master WHERE name=?",
                                  (name,)).fetchone()[0]
            if exists:
                continue
            conn.execute(sql)
            restored.append(name)
            log("restored trigger %s" % name)
    finally:
        conn.close()
    write_receipt(args.receipts, "restore-fts-triggers",
                  {"db": os.path.abspath(args.db), "restored": restored,
                   "source": path, "result": "OK"})
    print("RESTORED %d FTS trigger(s)" % len(restored))
    return 0


# --------------------------------------------------------------------------------------
# subcommand: vacuum
# --------------------------------------------------------------------------------------


def cmd_vacuum(args) -> int:
    ro = connect_ro(args.db)
    try:
        space = db_space(ro)
    finally:
        ro.close()
    free = free_bytes(args.db)
    need = 2 * space["content_bytes"] + TWO_GIB
    print("content %s, free %s, required 2x content + 2 GiB = %s"
          % (_h(space["content_bytes"]), _h(free), _h(need)))
    if free < need:
        payload = {"db": os.path.abspath(args.db), "db_space": space,
                   "free_bytes": free, "required_bytes": need, "result": "REFUSED"}
        write_receipt(args.receipts, "vacuum", payload)
        die("REFUSED: VACUUM rewrites every page through the WAL "
            "(hermes_state.py:15676-15683). Peak need is ~2x the post-vacuum size; "
            "free %s < required %s." % (_h(free), _h(need)), code=1)

    conn = connect_rw(args.db, args.quiet_window, timeout=60.0)
    try:
        _assert_sole_writer(conn, args.db, args.allow_other_writers)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        t0 = time.time()
        conn.execute("VACUUM")
        dt = time.time() - t0
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = db_space(conn)
    finally:
        conn.close()
    payload = {"db": os.path.abspath(args.db), "before": space, "after": after,
               "free_before": free, "seconds": round(dt, 1),
               "db_sizes_after": db_file_sizes(args.db), "result": "OK"}
    write_receipt(args.receipts, "vacuum", payload)
    print("VACUUM OK in %.1f s: page_count %d -> %d, file %s -> %s"
          % (dt, space["page_count"], after["page_count"],
             _h(space["file_bytes_from_pages"]), _h(after["file_bytes_from_pages"])))
    return 0


# --------------------------------------------------------------------------------------
# subcommand: report
# --------------------------------------------------------------------------------------


def cmd_report(args) -> int:
    conn = connect_ro(args.db)
    try:
        space = db_space(conn)
        counts = {}
        for t in ("sessions", "messages", "session_model_usage", "system_prompts",
                  "session_turn_leases", "compression_locks"):
            try:
                counts[t] = conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = None
    finally:
        conn.close()

    snap = {
        "stage": args.stage,
        "db": os.path.abspath(args.db),
        "at": iso(time.time()),
        "db_space": space,
        "db_sizes": db_file_sizes(args.db),
        "row_counts": counts,
        "free_bytes": free_bytes(args.db),
    }
    path = write_receipt(args.receipts, "report-%s" % args.stage, snap)

    before = latest_receipt(args.receipts, "report-before")
    after = latest_receipt(args.receipts, "report-after")
    print("=" * 78)
    print("REPORT stage=%s  db=%s" % (args.stage, snap["db"]))
    print("  page_count      %12d" % space["page_count"])
    print("  freelist_count  %12d" % space["freelist_count"])
    print("  file            %12s  wal %s  shm %s"
          % (_h(snap["db_sizes"]["db"]), _h(snap["db_sizes"]["wal"]),
             _h(snap["db_sizes"]["shm"])))
    for t, n in counts.items():
        print("  %-22s %10s" % (t, n))
    if before and after:
        print("-" * 78)
        print("BEFORE -> AFTER")
        for key in ("page_count", "freelist_count", "content_bytes"):
            b = before["db_space"][key]
            a = after["db_space"][key]
            print("  %-16s %14d -> %14d   (%+d)" % (key, b, a, a - b))
        b = before["db_sizes"]["db"]
        a = after["db_sizes"]["db"]
        print("  %-16s %14s -> %14s" % ("file bytes", _h(b), _h(a)))
        for t in before["row_counts"]:
            print("  %-22s %10s -> %10s" % (t, before["row_counts"][t],
                                            after["row_counts"].get(t)))
    print("=" * 78)
    print("receipt: %s" % path)
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="archive.py",
        description="Staged archive tool for ~/.hermes/state.db (MEASUREMENT.md §7b).")
    p.add_argument("--db", default=DEFAULT_DB, help="database path (default %s)" % DEFAULT_DB)
    p.add_argument("--receipts", default=None,
                   help="receipts directory (default: <db dir>/state-archive-receipts)")
    p.add_argument("--plan", default=None, help="PLAN.json path (default <receipts>/PLAN.json)")
    p.add_argument("--archive", default=None,
                   help="archive db path (default <db dir>/state-archive-YYYYMMDD.db)")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="hermes config.yaml path")
    p.add_argument(QUIET_FLAG, dest="quiet_window", action="store_true",
                   help="REQUIRED for every effectful subcommand")
    p.add_argument("--id-chunk", type=int, default=100,
                   help="session ids per chunked read (keeps every read short)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="read-only: compute and pin the doomed set")
    sp.add_argument("--days", type=float, default=7.0, help="idle cutoff in days (default 7)")
    sp.add_argument("--cutoff", type=float, default=None,
                    help="explicit unix-epoch cutoff (overrides --days)")
    sp.add_argument("--bytes-mode", choices=("auto", "exact", "sample", "none"),
                    default="auto",
                    help="auto = exact under 200k messages, sampled above")
    sp.add_argument("--sample-n", type=int, default=20000)
    sp.add_argument("--stale-locks-ok", action="store_true",
                    help="treat EXPIRED session_turn_leases / compression_locks rows as "
                         "non-protective (default: the literal §b2 filter protects any "
                         "row at all). Materially changes the doomed set — operator "
                         "decision, see DRYRUN.md.")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("preflight", help="verify every §b5 precondition")
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("backup", help="copy state.db + -wal + -shm together, sha256 each")
    sp.add_argument("--backup-dir", default=None)
    sp.add_argument("--allow-writers", action="store_true",
                    help="DANGEROUS: copy even though processes hold the db")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("copy", help="ATTACH the archive and INSERT...SELECT the doomed set")
    sp.add_argument("--resume", action="store_true",
                    help="allow inserting into an existing archive file")
    sp.add_argument("--no-archive-index", dest="archive_index", action="store_false",
                    default=True,
                    help="skip the lookup indexes the archive uses for /resume recovery")
    sp.add_argument("--fast-archive", action="store_true",
                    help="journal_mode=OFF + synchronous=OFF on the archive file "
                         "(faster bulk load; on failure delete the archive and redo)")
    sp.set_defaults(func=cmd_copy)

    sp = sub.add_parser("verify", help="counts + payload bytes + integrity_check + sha256")
    sp.add_argument("--quick", action="store_true", help="quick_check instead of integrity_check")
    sp.add_argument("--no-sha256", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("delete", help="chunked, resumable delete of the pinned doomed set")
    sp.add_argument("--batch-size", type=int, default=2000, help="messages per transaction")
    sp.add_argument("--sleep-ms", type=int, default=200, help="pause between batches")
    sp.add_argument("--session-batch", type=int, default=500)
    sp.add_argument("--max-batches", type=int, default=0, help="0 = run to completion")
    sp.add_argument("--log-every", type=int, default=25)
    sp.add_argument("--sweep-orphans", action="store_true",
                    help="also delete session_model_usage rows with no session at all")
    sp.add_argument("--skip-verify-gate", action="store_true",
                    help="do not require a PASSing verify receipt first")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("rebuild-fts", help="rebuild the external-content FTS5 indexes")
    sp.add_argument("--index", action="append", choices=list(FTS_INDEXES), default=None)
    sp.add_argument("--allow-other-writers", action="store_true")
    sp.set_defaults(func=cmd_rebuild_fts)

    sp = sub.add_parser("drop-fts-triggers",
                        help="fast path: drop the six FTS triggers (saves their DDL)")
    sp.add_argument("--allow-other-writers", action="store_true")
    sp.set_defaults(func=cmd_drop_fts_triggers)

    sp = sub.add_parser("restore-fts-triggers", help="recreate the saved FTS triggers")
    sp.add_argument("--triggers-file", default=None)
    sp.add_argument("--allow-other-writers", action="store_true")
    sp.set_defaults(func=cmd_restore_fts_triggers)

    sp = sub.add_parser("vacuum", help="VACUUM, only when 2x content + 2 GiB is free")
    sp.add_argument("--allow-other-writers", action="store_true")
    sp.set_defaults(func=cmd_vacuum)

    sp = sub.add_parser("report", help="page_count / freelist / sizes snapshot + diff")
    sp.add_argument("--stage", choices=("before", "after"), default="before")
    sp.set_defaults(func=cmd_report)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.receipts is None:
        args.receipts = os.path.join(
            os.path.dirname(os.path.abspath(args.db)) or ".", "state-archive-receipts")
    if args.cmd in EFFECTFUL and not args.quiet_window:
        die("`%s` is effectful and requires %s" % (args.cmd, QUIET_FLAG))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
