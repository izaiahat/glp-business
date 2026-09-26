#!/usr/bin/env python3
"""TEMPORARY guard: re-assert an orchestrator-assigned standing brief that seat_continuation.py parked.

Why this exists (2026-09-19): `scripts/seat_continuation.py` parks ANY seat with no UNMET register rows by
setting `brief: null`, including a brief the orchestrator assigned by hand. At 05:54Z that silently destroyed
the kael-sol orchestrator resume handoff. The correct fix is in that producer (leave briefs it did not
generate alone), but it is a PROTECTED ORACLE: changing it needs an operator decision receipt naming the file
plus an APPROVED independent review. Until that lands (Triage candidate), this guard restores the assignment.

It is deliberately narrow: one seat, one brief path, both given on the command line; it only ever writes when
the seat's brief is null AND the brief file still exists; it never invents work, never dispatches, never
changes any other field. Remove it when the producer fix is reviewed and applied.
"""
import argparse, json, os, sys
from pathlib import Path

STANDING = Path("/home/ubuntu/business/ops/seat-standing-work.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seat", required=True)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    brief = Path(a.brief)
    if not brief.is_file():
        print(json.dumps({"status": "SKIP", "reason": "brief file missing", "brief": str(brief)})); return 0
    d = json.loads(STANDING.read_text())
    seat = d.get("seats", {}).get(a.seat)
    if seat is None:
        print(json.dumps({"status": "SKIP", "reason": "unknown seat", "seat": a.seat})); return 0
    if seat.get("brief"):
        print(json.dumps({"status": "OK", "seat": a.seat, "brief": seat["brief"]})); return 0
    if not a.apply:
        print(json.dumps({"status": "WOULD_RESTORE", "seat": a.seat, "brief": str(brief)})); return 0
    seat["brief"] = str(brief)
    seat.pop("parked_reason", None)
    tmp = STANDING.with_suffix(".guard.tmp")
    tmp.write_text(json.dumps(d, indent=1) + "\n")
    os.replace(tmp, STANDING)
    print(json.dumps({"status": "RESTORED", "seat": a.seat, "brief": str(brief)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
