#!/usr/bin/env python3
"""Close the loop the dispatcher leaves open: turn needs_new_work into the next brief.

WHY THIS EXISTS
seat_autodispatch.py correctly detects a seat that finished its brief and reports
`needs_new_work` -- and then the chain ends, because cutting the next brief was the
orchestrator's job and the orchestrator is a human-paced agent that is not always looking.
Observed five times: the cron fired every two minutes, logged needs_new_work every time, and
three lanes sat idle until the operator noticed. The monitor was never the defect; the missing
CONSUMER was.

WHAT IT DOES AND DOES NOT DO
This is CONTINUATION, never admission. It regenerates a conforming brief for rows the register
ALREADY assigns to that seat (`matrix_owner`), on an issue the orchestrator ALREADY admitted. It
invents no scope, promotes no row, and touches no issue state. If a seat has no remaining rows it
is PARKED with that reason rather than handed invented work.

WHY IT DOES NOT THRASH
A brief is only written when its content DIFFERS from what the seat is already pointed at, so a
seat blocked on an unchanged condition is never re-sent the same work -- the exact failure the
linear-orchestrator skill records ("kael-sol re-dispatched three times into a build lock I had
set"). When the remaining set changes, or a previously named blocker clears and the rows move,
the content changes and the seat is re-armed automatically.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from seat_autodispatch import provider_depleted as _provider_depleted
import sys

BUSINESS = Path("/home/ubuntu/business")
STANDING = BUSINESS / "ops/seat-standing-work.json"
REGISTER = BUSINESS / "reports/ops/glp38-20260915/operator-note-closeout-register.json"
TEMPLATE = BUSINESS / "ops/linear/DISPATCH-TEMPLATE.md"
OUTDIR = BUSINESS / "reports/ops/glp38-20260915/continuation"
STATE = BUSINESS / "ops/seat-continuation-state.json"
SKILLS = {
    "kael-sol": "provider-registry-data-ops, affiliate-unit-economics, accessibility-browser-verification",
    "glp-design": "premium-landing-page-design, design-system-showcase, comparison-site-programmatic-seo",
    "glp-scout": "accessibility-browser-verification, comparison-site-programmatic-seo, quiz-funnel-cro",
}


def block(name: str) -> str:
    """Lift a block out of the canonical template so this producer cannot drift from it."""
    text = TEMPLATE.read_text().splitlines()
    if name == "router":
        return "\n".join(l for l in text if l.startswith("> "))
    start = text.index("COMPLETION") + 1
    end = next(i for i, l in enumerate(text) if l.startswith("DONE:"))
    return "\n".join(text[start:end])


def remaining_rows(seat: str) -> list[str]:
    register = json.loads(REGISTER.read_text())
    return [r["id"] for r in register["rows"]
            if r.get("matrix_status") == "UNMET"
            and (r.get("matrix_owner") or "").split(";")[0].strip() == seat]


ACCEPTANCE_CACHE = BUSINESS / "ops/linear/acceptance-cache.json"


def issue_clause(issue: str = "GLP-38") -> str:
    """The first sentence of the issue's own ## Acceptance, verbatim, from the orchestrator's cache.

    H10 (review 2026-09-16): every brief this producer composed was REFUSED by brief_clause_gate ("quotes no
    sentence of the issue's ## Acceptance verbatim"), so the needs_new_work consumer could never deliver. A
    brief names the clause it serves; this one serves GLP-38's first clause as ENABLING work.
    """
    import re as _re
    entry = (json.loads(ACCEPTANCE_CACHE.read_text()) if ACCEPTANCE_CACHE.exists() else {}).get(issue) or {}
    text = (entry.get("acceptance") or "").strip()
    if not text:
        raise RuntimeError(f"{issue} acceptance is not in {ACCEPTANCE_CACHE}; a brief cannot be bound to a clause it cannot read")
    return next(s.strip() for s in _re.split(r"(?<=[.!?])\s+", text) if s.strip())


def compose(seat: str, rows: list[str]) -> str:
    result = f"/home/ubuntu/business/reports/ops/glp38-20260915/continuation/{seat}/RESULT.json"
    clause = issue_clause("GLP-38")
    return f"""LINEAR: GLP-38 — Perform Fable's full emulation fidelity and operator-note closeout. Read the issue first. Standard: ops/linear/WORKER-STANDARD.md.
MODEL: gpt-6.1-sol
PROVIDER: openai-codex
REASONING: high
GOAL
This clause of GLP-38's Acceptance is what your rows serve, quoted verbatim: "{clause}"
ENABLING WORK FOR: "{clause}" - this seat reads the actual desktop/mobile candidate for its own rows and records the read-back effect that Fable then reads and signs against.
Continue your own assigned rows in reports/ops/glp38-20260915/operator-note-closeout-register.json to their read-back effect: {', '.join(rows)}.
Read each row's OWN `remaining` field first and match the verb to it: a row whose `remaining` says source is authored and a readback is owed needs the READBACK RUN, not a reimplementation. This brief was regenerated from the register because your previous brief's rows changed; it adds no scope beyond the rows listed above.
ACCEPTANCE
- `python3 scripts/register_writer.py show {rows[0]}` → prints `matrix_status` MET with its stated evidence, or UNMET naming the exact condition, its file:line or receipt, and who owns removing it. The same holds for every other row listed in GOAL.
- `python3 scripts/register_writer.py verify` → exit 0, invariants and audit chain intact with the live sha matching. Every register write goes through this writer; a hand-edited register fails this check.
- `python3 scripts/result_lint.py {result}` → exit 0, with every `## Acceptance` sentence of GLP-38 quoted verbatim in `acceptance_clauses` and a verdict for each.
BASELINE
Regenerated from the register's live state. Your remaining UNMET rows are exactly: {', '.join(rows)}. If a row's blocker is owned by another seat or by Fable, say so per row and continue every other row to its effect.
NOT IN SCOPE
- Other seats' rows, rows marked OUT OF ACCEPTED SCOPE, and rows that require Fable's signature.
- The evidence adoption path (scripts/glp_evidence_*), ACCEPTED_CAPTURE_PINS and src/mystery-shop.mjs: Fable owns those.
FILES
Touch: /home/ubuntu/business/reports/ops/glp38-20260915/ and the source surfaces your own rows name. Do not touch: /home/ubuntu/business/scripts/glp_evidence_refresh_cycle.py /home/ubuntu/business/scripts/glp_evidence_ledger.mjs /home/ubuntu/business/ops/evidence-freshness/adoptions.jsonl
TIER: T1
TOOLS
Owning skills: {SKILLS.get(seat, 'provider-registry-data-ops')}
ROUTER BLOCK (paste verbatim; the words "skills enforced" do not substitute for it)
{block('router')}
DELEGATION
Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes and preserve one writer per file.
COMPLETION
{block('completion')}
DONE: write {result} (new) in the shape of ops/linear/RESULT-SCHEMA.json, then print `GLP-38 COMPLETE {result}`.
"""


def probe(seat: str, standing: dict) -> str:
    """Run this seat's optional rearm probe. Exit 0 means a previously named blocker has cleared.

    A probe is what distinguishes "re-send the same work" (thrash) from "your blocker is gone, go".
    Without one, a seat blocked on an unchanged condition with an unchanged row set is held.
    """
    command = (standing["seats"].get(seat) or {}).get("rearm_probe")
    if not command:
        return "none"
    try:
        done = subprocess.run(["bash", "-lc", command], cwd=str(BUSINESS),
                              capture_output=True, text=True, timeout=180)
        return "pass" if done.returncode == 0 else "fail"
    except Exception:
        return "fail"


def lint_ok(path: Path) -> list:
    sys.path.insert(0, str(BUSINESS / "scripts"))
    import brief_lint
    return brief_lint.lint(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seats", nargs="*", help="limit to these seats (default: every seat needing work)")
    ap.add_argument("--apply", action="store_true", help="write briefs and re-point seats; otherwise report only")
    args = ap.parse_args()

    standing = json.loads(STANDING.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    report = {"schema": "seat-continuation-v1", "rearmed": [], "parked": [], "unchanged": [],
              "held": [], "refused": []}
    seats = args.seats or [s for s in standing["seats"] if s in SKILLS]

    for seat in seats:
        if seat not in standing["seats"]:
            report["refused"].append({"seat": seat, "reason": "not a configured seat"})
            continue
        rows = remaining_rows(seat)
        current = standing["seats"][seat].get("brief")
        if not rows:
            if current is not None and args.apply:
                standing["seats"][seat]["brief"] = None
                standing["seats"][seat]["parked_reason"] = "No UNMET rows remain for this seat in the register."
            report["parked"].append({"seat": seat, "reason": "no UNMET rows own by this seat"})
            continue
        previous = state.get(seat) or {}
        # Capacity-blind rearm (2026-09-16): minutes after four seats were parked on a depleted
        # openai-codex pool, this probe went green on three of them and offered to re-point them
        # straight into HTTP 429. Row state and blocker state are not the only preconditions for
        # dispatch; the seat's provider must have SOMETHING left. Shared with seat_autodispatch so the
        # two consumers cannot disagree about what "depleted" means.
        depleted = _provider_depleted((standing["seats"][seat] or {}).get("provider") or "openai-codex")
        if depleted:
            report["held"].append({"seat": seat, "rows": rows, "probe": "capacity", "reason": depleted})
            continue
        blocker = probe(seat, standing)
        rows_changed = previous.get("rows") != rows
        blocker_cleared = previous.get("probe") == "fail" and blocker == "pass"
        # A FAILING probe means this seat's work is blocked right now. Re-arming it because its row
        # SET changed just sends it into the same wall: observed 2026-09-16, when WO-06 settling moved
        # glp-design from 5 rows to 4 and re-armed it into work that still needed the build. A row-set
        # change is only a reason to dispatch when the seat can actually act.
        if blocker == "fail" and not blocker_cleared:
            report["held"].append({"seat": seat, "rows": rows, "probe": blocker,
                                   "reason": "blocker still failing; a changed row set is not a reason "
                                             "to dispatch into it"})
            continue
        if previous and not rows_changed and not blocker_cleared:
            # Same rows and the blocker has not cleared: re-sending is thrash, not dispatch.
            report["held"].append({"seat": seat, "rows": rows, "probe": blocker,
                                   "reason": "same rows and blocker unchanged; a resend would thrash"})
            continue
        body = compose(seat, rows)
        target = OUTDIR / f"{seat}.md"
        if current == str(target) and target.exists() and target.read_text() == body and not blocker_cleared:
            report["unchanged"].append({"seat": seat, "rows": rows,
                                        "reason": "same rows; re-sending would be thrash"})
            continue
        if not args.apply:
            report["rearmed"].append({"seat": seat, "rows": rows, "brief": str(target), "applied": False})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        problems = lint_ok(target)
        if problems:
            # A brief this producer cannot make conformant is never pointed at a seat.
            report["refused"].append({"seat": seat, "brief": str(target),
                                      "reason": "generated brief fails brief_lint",
                                      "problems": [str(p) for p in problems][:5]})
            continue
        standing["seats"][seat]["brief"] = str(target)
        standing["seats"][seat].pop("parked_reason", None)
        state[seat] = {"rows": rows, "probe": blocker}
        report["rearmed"].append({"seat": seat, "rows": rows, "brief": str(target),
                                  "probe": blocker, "applied": True,
                                  "trigger": "rows changed" if rows_changed else "blocker cleared"})

    if args.apply:
        for seat in seats:
            if seat in standing["seats"] and seat not in state:
                state.setdefault(seat, {"rows": remaining_rows(seat), "probe": probe(seat, standing)})
        tmp = STANDING.with_suffix(".tmp")
        tmp.write_text(json.dumps(standing, indent=1) + "\n")
        os.replace(tmp, STANDING)
        stmp = STATE.with_suffix(".tmp")
        stmp.write_text(json.dumps(state, indent=1) + "\n")
        os.replace(stmp, STATE)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
