#!/usr/bin/env python3
"""Keep the four TUI seats working without a human noticing they stopped.

Why (operator, 2026-09-15, repeatedly): every previous monitor NOTIFIED and then
relied on the orchestrator to restart it and re-dispatch. That depends on
discipline the orchestrator kept failing to supply, so seats sat idle for long
stretches and the operator had to spot it every time. This removes the human
from the loop: cron runs it, it sends the seat's standing brief itself.

STATE-DRIVEN, NOT CLOCK-DRIVEN (2026-09-16 rebuild)
---------------------------------------------------
The operator's standing order is completion, not clocks. An earlier version gated
dispatch on three wall-clock timers and they produced exactly the failures the
order predicts:

  * COOLDOWN_S held seats idle while they still had rows ("cooldown 659s
    remaining" x8 in one hour), and re-sent an identical brief three times in 32
    minutes purely because a clock expired.
  * a `no_progress_strikes` multiplier made the required wait grow by 300s per
    4-minute tick, so the wait outran wall-clock time permanently and strikes
    were never reset on a successful dispatch - a one-way deadlock, not a backoff.
  * that strike gate also sat ABOVE the brief-digest check, so brand-new work
    from the orchestrator could not wake a struck seat.
  * the progress oracle was one hard-coded GLP-38 artifact path applied to all
    four seats, including the seat working GLP-37. When that issue closes the
    signature freezes forever and every seat deadlocks silently. CLAUDE.md bans
    exactly that shape: PERSISTENT PATH ONLY, no hand-picked artifact as a
    permanent mechanism.

All of it is gone. `decide()` below is a TOTAL, PURE function of observed state -
no counter, no timer, no clock reading anywhere in the decision path - and the
liveness invariant it guarantees is proved in tests/test_seat_autodispatch.py:

    NEW WORK ALWAYS WINS. A brief whose sha256 differs from the one last sent to
    that seat is dispatched on the very next idle sample, from EVERY reachable
    seat state, except when the same dispatched path already produced its own
    freshly changed terminal RESULT after a short brief amendment. In that case
    completion is observed, never sent. No persisted value can suppress a genuinely
    new assignment at another path, so no state is terminal for the seat.

    dispatch  iff  the pane was not busy on the PREVIOUS run
                   AND the pane is not busy on THIS run      (idle is sustained)
                   AND the brief's sha256 differs from the one last sent

PER-SEAT PROGRESS ORACLE (2026-09-16 pass 2)
--------------------------------------------
When the digest is unchanged, "did this seat make progress?" is answered against
THAT SEAT'S OWN artifact, never a shared one: the brief itself names its RESULT
contract (`DONE: write <abs RESULT.json>` in the canonical template, `RESULT:
<abs>` in the step-7 form - the same grammar scripts/brief_lint.py:622 parses).
The dispatcher resolves it from the seat's own brief at read time, so no issue
key is embedded in code, an orchestrator re-point moves the oracle with it, and a
closed issue can never freeze a signature for seats that do not write it.

That splits one blind "unchanged" bucket into three honest outcomes:

  * result written for this brief (its bytes changed since we dispatched, or it
    carries a terminal status) -> needs_new_work. The seat finished; only the
    orchestrator can cut the next assignment. Terminal for THIS brief, recorded
    once.
  * no result and we have not re-armed this exact brief yet -> rearm: send the
    same pointer ONE more time, bounded by state (never by a clock). The seat is
    idle at a prompt and produced nothing, so the assignment was never consumed -
    a send that never landed is the one case where re-sending is the repair, and
    a seat that HAS already read it treats the resend as a documented no-op.
  * no result and we already re-armed this brief -> stalled. Recorded and left
    alone; a third send would be the thrash this file exists to prevent.
  * brief declares no RESULT path -> the oracle is UNKNOWN, which fails closed to
    needs_new_work with that stated plainly. Never re-armed on a guess.

Guards kept, because an auto-dispatcher that thrashes is worse than one that sleeps:
  * BUSY seats are never touched (prompt line must lack 'Ctrl+C to interrupt'),
    and idle must be sustained across two consecutive runs. The "second sample"
    is the PREVIOUS cron run, which removed the 45s blocking sleep that used to
    let one run outlive its own cron interval.
  * LOAD GATE for genuine host saturation only.
  * brief=null parks a seat deliberately, and a missing brief file is reported,
    never guessed.
  * a brief carrying a `SUPERSEDED-BY:` header is refused: a stale pointer must
    not re-arm a retired instruction. (Observed 2026-09-16: cron re-sent a brief
    whose orchestrator build gate had been revoked 14 minutes earlier.)
  * ONE RUN AT A TIME via flock, and state is persisted the instant a send is
    recorded, so a crash can never lose a dispatch and cause a duplicate send.
    `tui_send.sh` exit 3 means submitted; exit 2 means refused before typing and
    nothing was typed.

It only ever sends an EXISTING brief through the registered pointer transport
(`scripts/tui_send.sh`). It writes no briefs and invents no work: deciding the
next assignment stays the orchestrator's job. It NEVER pages the operator - an
exhausted seat is normal operation, and "no status pings" is a standing order.
The signal lives in this run's JSON and logs/seat-autodispatch.log.
"""
from __future__ import annotations

import fcntl
from datetime import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from brief_model_effort import brief_route, status_route

BUSINESS = Path("/home/ubuntu/business")
CONFIG = BUSINESS / "ops/seat-standing-work.json"
STATE = BUSINESS / "ops/seat-autodispatch-state.json"
# The append-only record of record that scripts/linear_state_reconciler.py reads to keep
# Linear current (operator 2026-09-15: "linear isnt updating ... i can always see our
# current state"). Its documented writer had no callers, so the ledger froze on 09-15
# while dispatch continued; the reconciler then ran on a 21-hour-stale reality source.
# The live dispatcher writes it now - that is the registered path, not a new one.
LEDGER = BUSINESS / "ops/linear/dispatch-ledger.jsonl"
# Orchestrator-owned consumer can tail this durable transition stream; the dispatcher
# still never invents a new assignment or pages the operator.
EVENTS = BUSINESS / "ops/orchestrator-events.jsonl"
LOG = BUSINESS / "logs/seat-autodispatch.log"
RUNLOCK = BUSINESS / "ops/seat-autodispatch.run.lock"
PROC = Path("/proc")
# WORKER-STANDARD 7's load-8 ceiling governs admitting CPU-HEAVY (browser) work, and the
# SEATS enforce it themselves before starting browser runs. Duplicating it here double-gated
# the fleet: on 2026-09-16 an 8-agent audit workflow pushed one-minute load to 31.7 and this
# dispatcher refused ALL dispatch for ~12 minutes, starving four seats that had non-browser
# rows they could have worked. Keep a ceiling for genuine saturation only; let the seat decide
# whether ITS next step is browser work.
LOAD_CEILING = float(os.environ.get("SEAT_AUTODISPATCH_LOAD_CEILING", "24"))
# Only these keys are real state; anything else in the file is a retired mechanism.
# None of these suppress a NEW brief in decide(); an independently observed invalid
# destination still blocks the SAME address until standing tuple changes or the
# registered read-only tui_send.sh --check proves the original tuple healthy again.
STATE_KEYS = ("last_dispatch", "last_brief_digest", "last_brief_path", "last_result_path",
              "last_result_sig", "last_pane_busy", "needs_work_alerted", "rearm_for_digest",
              "last_event_keys", "invalid_address")

# The brief's own RESULT contract, per seat. Same grammar as scripts/brief_lint.py:622
# (_RESULT_PATH_RE): the canonical template writes `DONE: write <abs>` and the step-7 form
# writes `RESULT: <abs>`. Parsing the seat's own brief is what keeps the progress oracle
# per-seat with no issue key in this file.
RESULT_PATH_RE = re.compile(r"^(?:DONE:\s*write\s+|RESULT:\s*)(/\S+RESULT\.json)", re.M)
# A seat is done with a brief once its RESULT reaches ANY terminal status, not only COMPLETE
# (brief_lint.py:619 learned the same lesson on GLP-34: a BLOCKED RESULT is finished work).
TERMINAL_STATUS = ("COMPLETE", "PASS", "BLOCKED", "CANCELED", "CANCELLED")


def pane_busy(pane: str) -> bool | None:
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-3"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return None
    return "Ctrl+C to interrupt" in lines[-1]


def observe_busy_route(pane: str) -> tuple[str, str | None] | None:
    """Read the native status bar only; never infer a model from conversation text."""
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-5"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return status_route(out.stdout) if out.returncode == 0 else None


def busy_model_mismatch(seat: dict, busy: bool | None, observed: tuple[str, str | None] | None) -> dict | None:
    """Only a readable declared brief and a native busy status can establish a mismatch."""
    if busy is not True or not observed or not seat.get("brief"):
        return None
    try:
        raw = Path(seat["brief"]).read_bytes()
    except OSError:
        return None
    expected = brief_route(raw.decode("utf-8-sig", "replace"))
    if not expected or (observed[0] == expected[0] and observed[1] in (None, expected[1])):
        return None
    return {"brief": str(seat["brief"]), "brief_digest": hashlib.sha256(raw).hexdigest()[:16],
            "expected_model": expected[0], "expected_effort": expected[1],
            "observed_model": observed[0], "observed_effort": observed[1] or "unknown"}


def superseded_by(body: str) -> str | None:
    """Return the successor named by a `SUPERSEDED-BY:` header, if the brief carries one."""
    for line in body[:4096].splitlines():
        bare = line.strip().lstrip("#*_> -").strip()
        if bare.upper().startswith("SUPERSEDED-BY:"):
            return bare.split(":", 1)[1].strip()[:120] or "(unnamed successor)"
    return None


def result_contract(body: str) -> str | None:
    """The absolute RESULT.json path this brief obliges its seat to write, or None."""
    m = RESULT_PATH_RE.search(body)
    return m.group(1) if m else None


def result_state(path: str | None) -> dict:
    """Signature + status of ONE seat's own RESULT artifact.

    sig is a content witness, never a timestamp comparison: 'absent' when the file is not
    there, 'unreadable' when it cannot be read, else a hash of its bytes. Progress is then
    'sig differs from the sig captured when we dispatched', which is pure state.
    """
    if not path:
        return {"path": None, "sig": None, "status": None}
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return {"path": path, "sig": "absent", "status": None}
    except OSError:
        return {"path": path, "sig": "unreadable", "status": None}
    status = issue = result_brief = None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        status = str(data.get("status", "")).upper() or None
        issue, result_brief = data.get("issue"), data.get("brief")
    except (ValueError, AttributeError):
        pass
    return {"path": path, "sig": hashlib.sha256(raw).hexdigest()[:16],
            "status": status, "issue": issue, "brief": result_brief}


REQUIRED_SEAT_KEYS = ("pane", "tmux", "session", "issue")


def seat_problem(seat) -> str | None:
    """A malformed seat entry is a NAMED skip, never a traceback that strands the other seats."""
    if not isinstance(seat, dict):
        return "config entry is not an object"
    missing = [k for k in REQUIRED_SEAT_KEYS if not seat.get(k)]
    return f"config entry missing {', '.join(missing)}" if missing else None

def current_pane_session(name: str, seat: dict, seats: dict) -> str | None:
    """Read the session file owned by this exact seat's live tmux process tree."""
    pane, tmux_session = seat["pane"], seat["tmux"]
    if sum(s.get("pane") == pane or s.get("tmux") == tmux_session
           for s in seats.values() if isinstance(s, dict)) != 1 or seats.get(name) is not seat:
        return None
    try:
        row = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pane_id}|#{session_id}|#{pane_pid}|#{pane_dead}"],
            capture_output=True, text=True, timeout=20)
        if row.returncode != 0:
            return None
        actual, session, pid, dead = row.stdout.strip().split("|")
        if (actual, session, dead) != (pane, tmux_session, "0"):
            return None
        pending, seen, paths = [int(pid)], set(), set()
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            env = (PROC / str(child) / "environ").read_bytes().split(b"\0")
            paths.update(item.split(b"=", 1)[1].decode() for item in env
                         if item.startswith(b"HERMES_TUI_ACTIVE_SESSION_FILE="))
            pending.extend(map(int, (PROC / str(child) / "task" / str(child) / "children").read_text().split()))
        if len(paths) != 1:
            return None
        sid = json.loads(Path(paths.pop()).read_text())["session_id"]
        return sid if isinstance(sid, str) and re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", sid) else None
    except (OSError, ValueError, KeyError, UnicodeError):
        return None

def save_config_session(config: dict, name: str, sid: str, preimage: bytes) -> bytes:
    """The dispatcher's atomic writer; refuse to overwrite an orchestrator edit."""
    updated = json.loads(preimage)
    updated["seats"][name]["session"] = sid
    if CONFIG.read_bytes() != preimage:
        raise ValueError("standing work changed during session recovery")
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=2) + "\n")
    if CONFIG.read_bytes() != preimage:
        tmp.unlink(missing_ok=True)
        raise ValueError("standing work changed during session recovery")
    os.replace(tmp, CONFIG)
    config["seats"][name]["session"] = sid
    return CONFIG.read_bytes()


CAPACITY_REL = "ops/subscription-capacity/latest.json"
CAPACITY = Path(BUSINESS) / CAPACITY_REL   # module attr: tests repoint it like STATE/LEDGER
CAPACITY_MAX_AGE_S = int(os.environ.get("SEAT_AUTODISPATCH_CAPACITY_MAX_AGE_S", "7200"))


def provider_depleted(provider: str, *, capacity_path: Path | None = None, now: float | None = None) -> str | None:
    """Reason string when EVERY account of `provider` reads depleted in the live collector, else None.

    2026-09-16: all four openai-codex accounts hit 0% and the seats returned HTTP 429. This dispatcher
    kept sending pointers into the 429, and the continuation rearm probe went green minutes after the
    seats were parked, because nothing on the dispatch path ever asked whether the seat's provider had
    any capacity. A stale or unreadable collector is NOT treated as depleted: failing closed here would
    silence the fleet on a collector hiccup, which is its own outage. It is treated as unknown, and the
    dispatch proceeds - the seat's own 429 is then the signal, exactly as before.
    """
    # Looked up on the module at call time (like STATE/LEDGER) so an isolated test world repoints
    # it and never reads the live collector; the live fleet always does.
    path = Path(capacity_path) if capacity_path else Path(CAPACITY)
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        collected = datetime.fromisoformat(str(doc.get("collected_at_utc", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    if (now if now is not None else time.time()) - collected > CAPACITY_MAX_AGE_S:
        return None
    rows = [r for r in (doc.get("records") or []) if r.get("provider") == provider]
    if not rows:
        return None
    if all(r.get("capacity_status") == "depleted" for r in rows):
        resets = sorted(str(r.get("weekly_reset_at_utc") or "")[:16] for r in rows)
        return (f"provider {provider} depleted on every account ({len(rows)}); "
                f"earliest reset {resets[0] or 'unknown'}; not sending into a 429")
    return None


def read_brief(seat: dict) -> dict:
    """Resolve everything the decision needs from the seat's OWN brief. IO lives here."""
    brief = seat.get("brief")
    if not brief:
        return {"ok": False, "reason": "parked (brief=null)"}
    # The four TUI seats run on the Hermes delegation provider; a seat may declare its own.
    depleted = provider_depleted(seat.get("provider") or "openai-codex")
    if depleted:
        return {"ok": False, "reason": depleted}
    path = Path(brief)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"ok": False, "reason": f"brief missing: {brief}"}
    except OSError as exc:
        return {"ok": False, "reason": f"brief unreadable: {exc}"}
    body = raw.decode("utf-8", "replace")
    successor = superseded_by(body)
    if successor:
        return {"ok": False, "reason": (
            f"brief is superseded by {successor}; refusing to re-arm a retired instruction. "
            f"Point ops/seat-standing-work.json at the successor.")}
    # The brief names its own RESULT contract; `result` in the config is only a fallback for a
    # brief that declares none. No default is guessed: an unknown oracle stays unknown.
    contract = result_contract(body) or seat.get("result")
    return {"ok": True, "path": path, "digest": hashlib.sha256(raw).hexdigest()[:16],
            "issue": seat.get("issue"), "result": result_state(contract)}


def result_binds(brief: dict, res: dict, *, require_identity: bool = False) -> bool:
    """An explicitly foreign RESULT is never this seat's completion.

    Legacy same-digest artifacts may omit identity; after brief edits, both the issue
    and the exact dispatched path must be present to distinguish a real completion.
    """
    path, issue = str(brief["path"]), brief.get("issue")
    if require_identity and (res.get("brief") != path or res.get("issue") != issue or not issue):
        return False
    return (res.get("brief") in (None, path)
            and (not issue or res.get("issue") in (None, issue)))


def decide(st: dict, prev_busy: bool | None, busy: bool | None, brief: dict) -> dict:
    """TOTAL, PURE decision for one seat. No IO, no clock, no counter.

    Returns {"action": dispatch|rearm|needs_new_work|stalled|skip, "reason": str,
             "progress": str|None}. Every input combination lands in exactly one action,
             and the order below is load-bearing: a genuinely new pointer is above every
             suppression. Only a fresh, identity-bound RESULT for the already-dispatched
             path takes precedence over an in-place brief edit.
    """
    if busy is None:
        return {"action": "skip", "reason": "pane unreadable", "progress": None}
    if busy:
        return {"action": "skip", "reason": "busy", "progress": None}
    if prev_busy is not False:
        return {"action": "skip", "progress": None,
                "reason": "idle on this sample only; needs a second consecutive idle sample"}
    if not brief.get("ok"):
        return {"action": "skip", "reason": brief.get("reason", "brief unusable"), "progress": None}

    digest = brief["digest"]
    res = brief.get("result") or {}
    sig, base = res.get("sig"), st.get("last_result_sig")
    readable = sig not in (None, "absent", "unreadable")
    # A short amendment changes the digest but not the already-delivered assignment.
    # Observe a completed RESULT before attempting to send those now-refused bytes,
    # only when the last successful send pinned this very path and RESULT contract.
    # Legacy state without those witnesses fails closed to the normal send guard.
    if (st.get("last_brief_digest") != digest
            and st.get("last_brief_path") == str(brief["path"])
            and st.get("last_result_path") == res.get("path")
            and base is not None and readable and sig != base
            and res.get("status") in TERMINAL_STATUS
            and result_binds(brief, res, require_identity=True)):
        return {"action": "needs_new_work",
                "progress": f"result written at {res['path']} (status={res.get('status')})",
                "reason": "seat produced its RESULT for the dispatched brief before its amendment; "
                          "it needs a NEW assignment, not a resend"}
    # NEW WORK ALWAYS WINS. No old result at another path or from another issue
    # can hold a genuinely new pointer; the transport still admits all sends.
    if st.get("last_brief_digest") != digest:
        return {"action": "dispatch", "reason": "new brief digest", "progress": None}

    # Same brief as last sent. Judge progress against THIS seat's own RESULT artifact.
    if not res.get("path"):
        return {"action": "needs_new_work", "progress": "unknown: brief declares no RESULT path",
                "reason": "seat already received this exact brief and its progress cannot be "
                          "measured (no `DONE: write <abs RESULT.json>` / `RESULT:` line). "
                          "Failing closed: no resend, cut a conforming brief."}
    # Content change from the send baseline is the witness; the previous brief's
    # unchanged terminal RESULT is not progress. Legacy state with no baseline may
    # use a terminal status, but an explicitly foreign identity is never accepted.
    if readable and result_binds(brief, res) and (sig != base if base is not None else res.get("status") in TERMINAL_STATUS):
        return {"action": "needs_new_work",
                "progress": f"result written at {res['path']} (status={res.get('status')})",
                "reason": "seat produced its RESULT for this brief; it needs a NEW assignment, "
                          "not a resend"}
    seen = "unreadable" if sig == "unreadable" else ("absent" if sig == "absent" else "unchanged")
    if st.get("rearm_for_digest") != digest:
        return {"action": "rearm", "progress": f"RESULT {res['path']} {seen} since dispatch",
                "reason": "pointer was sent but the seat is idle with no RESULT for it; "
                          "re-arming this brief exactly once (bounded by state, not a clock)"}
    return {"action": "stalled", "progress": f"RESULT {res['path']} {seen} after re-arm",
            "reason": "seat is idle and produced no RESULT after a second pointer; a third "
                      "send would be thrash. The orchestrator owns this seat's next step."}


def save_state(state: dict) -> None:
    """Persist immediately after every recorded send; a lost send becomes a duplicate send."""
    pruned = {n: {k: v for k, v in s.items() if k in STATE_KEYS}
              for n, s in state.items() if isinstance(s, dict)}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned, indent=1))
    os.replace(tmp, STATE)


def ledger_append(row: dict) -> str | None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        return str(exc)
    return None


def event_append_once(st: dict, row: dict, identity: list[str | None]) -> str | None:
    """Append one transition per identity; recover an append-before-state crash from the log.

    Called under the run flock. State keeps only the most recent key per event kind; the
    append-only stream closes the gap if an older brief/address comes back later.
    Returns an error string on write/read failure; an unverified event is never marked seen.
    """
    key = json.dumps([row["kind"], row["seat"], *identity], separators=(",", ":"))
    seen = st.get("last_event_keys") or {}
    if seen.get(row["kind"]) == key:
        return None
    try:
        if EVENTS.exists():
            with EVENTS.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        if json.loads(line).get("event_key") == key:
                            st.setdefault("last_event_keys", {})[row["kind"]] = key
                            return None
                    except ValueError:
                        continue  # a torn/older row is not proof of this event
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row, event_key=key)) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        return str(exc)
    st.setdefault("last_event_keys", {})[row["kind"]] = key
    return None


def address_tuple(seat: dict) -> dict:
    return {k: seat[k] for k in ("pane", "tmux", "session")}


def session_mismatch_refusal(out: subprocess.CompletedProcess) -> bool:
    """Only exact destination-identity refusals latch; other exit-2 gates can recover."""
    if out.returncode != 2:
        return False
    text = (out.stdout or "") + "\n" + (out.stderr or "")
    return ("REFUSED: actual Hermes session mismatch:" in text
            or "REFUSED: pane/session mismatch or dead pane" in text)


def main() -> int:
    RUNLOCK.parent.mkdir(parents=True, exist_ok=True)
    lockfh = open(RUNLOCK, "w")
    try:
        fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"status": "SKIPPED", "reason": "another run holds the lock"}))
        lockfh.close()
        return 0
    try:
        return _run()
    finally:
        fcntl.flock(lockfh, fcntl.LOCK_UN)
        lockfh.close()


def _run() -> int:
    try:
        raw_config = CONFIG.read_bytes()
        config = json.loads(raw_config)
        cfg = config["seats"]
        if not isinstance(cfg, dict):
            raise ValueError("`seats` must be an object")
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": f"config unreadable: {exc}"}))
        return 1

    state = {}
    if STATE.exists():
        try:
            loaded = json.loads(STATE.read_text())
            if isinstance(loaded, dict):
                state = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except ValueError:
            state = {}

    now = time.time()
    load1 = os.getloadavg()[0]
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "load_1m": round(load1, 2),
              "dispatched": [], "needs_new_work": [], "stalled": [], "skipped": {},
              "session_recovered": []}
    unproven = set()
    for name, seat in cfg.items():
        if seat_problem(seat):
            continue
        sid = current_pane_session(name, seat, cfg)
        if sid is None:
            unproven.add(name)
            report["skipped"][name] = "native pane session unprovable"
            continue
        if sid == seat["session"]:
            continue
        check = ["bash", str(BUSINESS / "scripts/tui_send.sh"), "--check",
                 seat["pane"], seat["tmux"], sid]
        try:
            verified = subprocess.run(check, capture_output=True, text=True, timeout=30)
            if verified.returncode != 0:
                raise ValueError(f"native session check refused (exit {verified.returncode})")
            previous = seat["session"]
            raw_config = save_config_session(config, name, sid, raw_config)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            unproven.add(name)
            report["skipped"][name] = f"session recovery refused: {exc}"
            continue
        report["session_recovered"].append({"seat": name, "pane": seat["pane"],
                                            "tmux": seat["tmux"], "previous": previous,
                                            "session": sid, "verified_by": "tui_send.sh --check"})

    # One sample per run. The PREVIOUS run's sample is the second sample: idle must be
    # sustained across two observations, which is what the 45s sleep used to buy.
    broken = {n: seat_problem(s) for n, s in cfg.items()}
    prev_busy = {n: state.get(n, {}).get("last_pane_busy") for n in cfg}
    busy_now = {n: (None if broken[n] else pane_busy(s["pane"])) for n, s in cfg.items()}
    for n, b in busy_now.items():
        state.setdefault(n, {})["last_pane_busy"] = b

    # Observe all busy seats even when host load blocks new dispatch. This is a read-only
    # status-bar probe and never sends to a pane or changes the seat's execution route.
    for name, seat in cfg.items():
        if broken[name] or busy_now[name] is not True:
            continue
        mismatch = busy_model_mismatch(seat, True, observe_busy_route(seat["pane"]))
        if not mismatch:
            continue
        event = {"ts": report["at"], "kind": "seat_model_mismatch", "seat": name,
                 "issue": seat["issue"], **mismatch,
                 "reason": "busy seat status-bar model/effort differs from its declared brief route"}
        err = event_append_once(state[name], event, [mismatch["brief_digest"]])
        if err:
            report["skipped"][name] = f"model mismatch event write failed: {err}"

    if load1 > LOAD_CEILING:
        report["skipped"]["all"] = f"load {load1:.2f} over ceiling {LOAD_CEILING} (host saturation)"
        save_state(state)
        _emit(report)
        return 0

    for name, seat in cfg.items():
        if broken[name]:
            report["skipped"][name] = broken[name]
            continue
        if name in unproven:
            continue
        st = state.setdefault(name, {})
        address = address_tuple(seat)
        invalid = st.get("invalid_address")
        if invalid and any(invalid.get(k) != v for k, v in address.items()):
            # A new standing destination is independently eligible for the existing send gate.
            st.pop("invalid_address", None)
        elif invalid:
            # A repaired TUI (or a tmux server restart reusing %id) can restore the SAME
            # address. Probe only with the transport's read-only identity preflight; no
            # brief is sent while the latch remains unproven.
            cmd = ["bash", str(BUSINESS / "scripts/tui_send.sh"), "--check",
                   seat["pane"], seat["tmux"], seat["session"]]
            try:
                check = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                report["skipped"][name] = f"address check failed: {exc}"
                continue
            if check.returncode != 0:
                report["skipped"][name] = f"address still invalid (check exit {check.returncode})"
                continue
            event = {"ts": report["at"], "kind": "seat_address_valid", "seat": name,
                     "issue": seat["issue"], **address,
                     "reason": "read-only tui_send.sh --check confirmed the configured destination"}
            err = event_append_once(st, event, [address["pane"], address["tmux"], address["session"]])
            if err:
                report["skipped"][name] = f"address recovery event write failed: {err}"
                continue  # do not clear a latch without a durable transition signal
            st.pop("invalid_address", None)
            save_state(state)
        brief = read_brief(seat)
        d = decide(st, prev_busy.get(name), busy_now[name], brief)
        action = d["action"]

        if action == "skip":
            report["skipped"][name] = d["reason"]
            continue

        if action == "needs_new_work":
            res = brief.get("result") or {}
            res_path, res_sig = res.get("path"), res.get("sig")
            observed_digest = (st.get("last_brief_digest") if
                               st.get("last_brief_path") == str(brief["path"]) and
                               st.get("last_brief_digest") != brief["digest"]
                               else brief["digest"])
            event = {"ts": report["at"], "kind": "seat_needs_work", "seat": name,
                     "issue": seat["issue"], "brief": str(brief["path"]),
                     "brief_digest": observed_digest, "result_path": res_path,
                     "result_sig": res_sig, "reason": d["reason"]}
            # The same brief can receive a revised RESULT at the same path. Its content
            # witness distinguishes those completions without repeating unchanged bytes.
            err = event_append_once(st, event, [observed_digest, res_path, res_sig])
            if err:
                report["skipped"][name] = f"completion event write failed: {err}"

        if action in ("needs_new_work", "stalled"):
            entry = {"seat": name, "issue": seat["issue"], "brief": brief["path"].name,
                     "progress": d["progress"], "reason": d["reason"]}
            # Logged, never paged: cutting the next brief is the ORCHESTRATOR's action, and
            # the operator's standing order is "no status pings". An ultracode fix agent wired
            # an AMBER Pushover alert here on 2026-09-16; that is the wrong door.
            if st.get("needs_work_alerted") != brief["digest"]:
                st["needs_work_alerted"] = brief["digest"]
                save_state(state)
                entry["recorded"] = "logged (first observation for this brief; never paged)"
            else:
                entry["recorded"] = "logged (already recorded for this brief)"
            report["needs_new_work" if action == "needs_new_work" else "stalled"].append(entry)
            continue

        # A refused exact destination is not repaired by a changed brief: only a changed
        # standing pane/tmux/native-session tuple can make another send safe.
        if st.get("invalid_address"):
            continue
        # dispatch or rearm through the same named-seat gate used by manual sends.
        cmd = ["python3", str(BUSINESS / "scripts/seat_send.py"), name, str(brief["path"])]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            tail = (out.stdout or out.stderr).strip().splitlines()[-1][:120] if (out.stdout or out.stderr) else ""
        except (OSError, subprocess.SubprocessError) as exc:
            report["skipped"][name] = f"transport error: {exc}"
            continue
        # Wrapper exit 0 = native busy confirmed; 3 = pointer submitted but unconfirmed.
        # Both are effects and must be recorded before any rearm; 2 refused before the pointer.
        if out.returncode in (0, 3):
            st["last_dispatch"] = now
            st["last_brief_digest"] = brief["digest"]
            st["last_brief_path"] = str(brief["path"])
            st["last_result_path"] = (brief.get("result") or {}).get("path")
            # The oracle's baseline: progress is measured against the artifact AS IT WAS when
            # this pointer landed, so a RESULT left over from an earlier brief cannot read as
            # fresh progress, and a stale one cannot read as a stall forever.
            st["last_result_sig"] = (brief.get("result") or {}).get("sig")
            st.pop("needs_work_alerted", None)
            if action == "rearm":
                st["rearm_for_digest"] = brief["digest"]   # bounds the resend at exactly one
            else:
                st.pop("rearm_for_digest", None)
            save_state(state)          # record the send BEFORE anything else can fail
            err = ledger_append({
                "at_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                "brief": str(brief["path"]), "issue": seat["issue"],
                "lane": seat.get("lane"), "pane": seat["pane"], "seat": name,
                "via": "seat_autodispatch.py"})
            row = {"seat": name, "issue": seat["issue"], "brief": brief["path"].name,
                   "action": action}
            if d["progress"]:
                row["progress"] = d["progress"]
            if err:
                row["ledger_error"] = err
            report["dispatched"].append(row)
        elif session_mismatch_refusal(out):
            output_lines = ((out.stdout or "") + "\n" + (out.stderr or "")).strip().splitlines()
            reason = next((line[:500] for line in output_lines if
                           "REFUSED: actual Hermes session mismatch:" in line or
                           "REFUSED: pane/session mismatch or dead pane" in line),
                          output_lines[-1][:500])
            event = {"ts": report["at"], "kind": "seat_address_invalid", "seat": name,
                     "issue": seat["issue"], "brief": str(brief["path"]),
                     "brief_digest": brief["digest"],
                     "result_path": (brief.get("result") or {}).get("path"),
                     **address, "reason": reason}
            err = event_append_once(st, event, [address["pane"], address["tmux"], address["session"]])
            if err:
                report["skipped"][name] = f"address refusal, event write failed: {err}"
                continue  # do not latch an address without a durable orchestrator signal
            st["invalid_address"] = dict(address, reason=reason)
            save_state(state)
            report["skipped"][name] = f"address invalid (recorded once): {reason}"
        else:
            report["skipped"][name] = f"refused (exit {out.returncode}): {tail}"

    save_state(state)
    _emit(report)
    return 0


def _emit(report: dict) -> None:
    line = json.dumps(report)
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
