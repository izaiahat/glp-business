"""One operator decision must record as one decision, whatever its id is spelled.

2026-08-27.  Two live events (evt_4b134fc4..., evt_b987427f...) each carried TWO
operator_decisions rows for a single operator reply.  The rows agreed on event,
card, actor, action and card revision and differed only in decision_message_id:
one bare ("1540483509410340917"), one suffixed with the event id
("1540483509410340917:evt_4b134fc4...").  The control plane chose the spelling
from len(commands) on the sync pass, so two passes spelled one reply two ways,
derived two decision_ids, and INSERT OR IGNORE dedupes on decision_id -- so both
landed.  The reconciler's terminal check requires exactly one row and would have
raised operator_handoff_terminal_disposition_invalid on a decision the operator
had genuinely made.

Both halves are pinned here: the writer must not double-record, and the reader
must not be fooled by a pre-existing duplicate -- while a GENUINE conflict (a
different actor, or a different card revision) must still raise.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inbox_control_plane.control_plane as _cp


DDL = """
CREATE TABLE operator_decisions(
  decision_id TEXT PRIMARY KEY, event_id TEXT, channel_id TEXT, card_message_id TEXT,
  decision_message_id TEXT, actor_id TEXT, action TEXT, card_revision_sha256 TEXT,
  created_at TEXT
);
"""

EVENT = "evt_4b134fc4107ece0bb8be086d43c0"
CARD_REV = "92d29dbefb890800beb9defc0a1e09e5fa15e5d06359523cc316aa73591b55be"
ACTOR = "392067741500309517"
MSG = "1540483509410340917"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


# ---------------------------------------------------------------- reader half

def _distinct_decisions(conn: sqlite3.Connection, event_id: str) -> int:
    """The reconciler's content-based count (scripts/affiliate_approval_auto_reconciler.py)."""
    return len(conn.execute(
        "SELECT DISTINCT actor_id,card_revision_sha256 FROM operator_decisions "
        "WHERE event_id=? AND UPPER(action)='RESOLVED_EXTERNALLY'",
        (event_id,),
    ).fetchall())


def _insert(conn, decision_id, decision_message_id, actor=ACTOR, card_rev=CARD_REV):
    conn.execute(
        "INSERT OR IGNORE INTO operator_decisions VALUES(?,?,?,?,?,?,?,?,?)",
        (decision_id, EVENT, "chan", "card", decision_message_id, actor,
         "RESOLVED_EXTERNALLY", card_rev, "2026-08-21T22:15:03Z"),
    )
    conn.commit()


def test_two_spellings_of_one_decision_count_as_one():
    """The exact live shape: same actor, same card revision, two id spellings."""
    conn = _db()
    _insert(conn, "dec_3862ab4076c38cd5cd2311d4", f"{MSG}:{EVENT}")
    _insert(conn, "dec_bcdab3d5bebfa13c69c1f072", MSG)
    assert conn.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0] == 2, \
        "fixture must reproduce the two-row state actually found in the live DB"
    assert _distinct_decisions(conn, EVENT) == 1


def test_conflicting_card_revision_still_raises():
    """A decision against a DIFFERENT card revision is a real conflict, not a dupe."""
    conn = _db()
    _insert(conn, "dec_a", f"{MSG}:{EVENT}")
    _insert(conn, "dec_b", "9999", card_rev="0" * 64)
    assert _distinct_decisions(conn, EVENT) == 2


def test_conflicting_actor_still_raises():
    conn = _db()
    _insert(conn, "dec_a", f"{MSG}:{EVENT}")
    _insert(conn, "dec_b", "9999", actor="111111111111111111")
    assert _distinct_decisions(conn, EVENT) == 2


def test_no_decision_is_not_one_decision():
    """Absence must never satisfy the exactly-one check."""
    assert _distinct_decisions(_db(), EVENT) == 0


# ---------------------------------------------------------------- writer half

class _FakeLedger:
    """Runs the REAL control-plane helpers against an in-memory table.

    The methods are bound off the production Ledger class rather than
    reimplemented -- a test that copies the logic it is meant to guard would
    keep passing after the production code regressed.  Both helpers touch only
    self.conn, so a bare carrier object is a faithful host.
    """

    _canonical_decision_id = _cp.Ledger.__dict__["_canonical_decision_id"]
    _insert_operator_decision = _cp.Ledger.__dict__["_insert_operator_decision"]

    def __init__(self, conn):
        self.conn = conn


def _record(ledger, decision_message_id, actor=ACTOR, card_rev=CARD_REV):
    ledger._insert_operator_decision(
        event_id=EVENT, channel_id="chan", card_message_id="card",
        decision_message_id=decision_message_id, actor_id=actor,
        action="RESOLVED_EXTERNALLY", card_revision_sha256=card_rev,
        now="2026-08-21T22:15:03Z",
    )


def test_writer_records_one_row_across_both_spellings():
    """The regression itself: two sync passes, two spellings, ONE row."""
    conn = _db()
    ledger = _FakeLedger(conn)
    _record(ledger, f"{MSG}:{EVENT}")   # pass 1 parsed several commands
    _record(ledger, MSG)                # pass 2 parsed one -- old code inserted again
    assert conn.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0] == 1
    assert _distinct_decisions(conn, EVENT) == 1


def test_writer_still_records_a_genuinely_different_decision():
    """Dedup must not swallow a decision against a new card revision."""
    conn = _db()
    ledger = _FakeLedger(conn)
    _record(ledger, f"{MSG}:{EVENT}")
    _record(ledger, "9999", card_rev="0" * 64)
    assert conn.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0] == 2


def test_decision_id_is_independent_of_message_id_spelling():
    """The key must not carry the unstable field -- that was the root cause."""
    a = _FakeLedger._canonical_decision_id(EVENT, "RESOLVED_EXTERNALLY", CARD_REV)
    b = _FakeLedger._canonical_decision_id(EVENT, "RESOLVED_EXTERNALLY", CARD_REV)
    assert a == b
    assert _FakeLedger._canonical_decision_id(EVENT, "SEND", CARD_REV) != a, \
        "a different action is a different decision"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
