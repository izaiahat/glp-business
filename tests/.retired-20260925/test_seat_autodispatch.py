"""State-machine proof for scripts/seat_autodispatch.py.

The audit (2026-09-16) found the seat dispatcher's no-progress "backoff" was a permanent
deadlock: the required wait grew by 300s per 240s tick and strikes were never reset on a
successful dispatch, and the strike gate sat ABOVE the brief-digest check so new work could
not wake a struck seat. The only progress oracle was one hard-coded GLP-38 artifact applied
to all four seats, including the seat working GLP-37.

These tests pin the properties that make that class of failure unreachable:

  * `decide()` reads no clock and holds no counter, so nothing can ratchet.
  * a NEW brief digest dispatches from EVERY reachable seat state (liveness / no deadlock).
  * a frozen brief produces a BOUNDED number of sends that never grows with tick count.
  * progress is judged against the RESULT artifact the seat's OWN brief names.
  * an exhausted seat is logged, never paged ("no status pings").
"""
from __future__ import annotations

import ast
import fcntl
import importlib.util
import io
import itertools
import json
import os
import re
import sys
import tokenize
from pathlib import Path

import pytest

from brief_model_effort import brief_route, route_instruction, status_route

BUSINESS = Path("/home/ubuntu/business")


@pytest.fixture(autouse=True)
def _no_live_collector(tmp_path, monkeypatch):
    """Unit tests never consult the live capacity collector; the capacity tests set their own file."""
    import seat_autodispatch as _sa
    monkeypatch.setattr(_sa, "CAPACITY", tmp_path / "no-collector.json")
SRC = Path(os.environ.get("SEAT_AUTODISPATCH_TEST_SOURCE", BUSINESS / "scripts/seat_autodispatch.py"))

_spec = importlib.util.spec_from_file_location("seat_autodispatch", SRC)
SA = importlib.util.module_from_spec(_spec)
sys.modules["seat_autodispatch"] = SA
_spec.loader.exec_module(SA)

SOURCE = SRC.read_text()


def _executable_source() -> str:
    """The file's CODE only - comments and string literals stripped.

    The docstring deliberately records the retired mechanisms by name (that history is why
    they cannot come back), so a substring search over the raw file would be a prose test.
    These checks look at what the module actually executes.
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(SOURCE).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _literal_strings() -> list:
    """Every string literal the module carries EXCEPT docstrings - i.e. real values."""
    tree = ast.parse(SOURCE)
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


CODE = _executable_source()
LITERALS = _literal_strings()

NEW = "new0000000000000"
OLD = "old0000000000000"
RESULT_PATH = "/tmp/seat-autodispatch-test/RESULT.json"


def brief(digest=OLD, result_path=RESULT_PATH, sig="absent", status=None, ok=True, reason=""):
    if not ok:
        return {"ok": False, "reason": reason}
    return {"ok": True, "path": Path("/tmp/b.md"), "digest": digest,
            "result": {"path": result_path, "sig": sig, "status": status}}


# ---------------------------------------------------------------- no clock, no counter

def test_decide_reads_no_clock_and_keeps_no_counter():
    names = SA.decide.__code__.co_names
    assert "time" not in names and "monotonic" not in names and "getmtime" not in names
    # the retired mechanisms must not come back under any spelling (CODE only, not prose)
    for banned in ("COOLDOWN_S", "SETTLE_S", "no_progress_strikes", "last_register_sig",
                   "sleep", "strikes"):
        assert not re.search(r"\b%s\b" % banned, CODE), f"retired mechanism reappeared: {banned}"
    # no wall-clock value may enter the decision path: STATE_KEYS carries exactly one
    # timestamp (last_dispatch) and decide() never looks at it.
    assert "last_dispatch" not in SA.decide.__code__.co_consts


def test_no_issue_specific_artifact_is_hard_coded():
    """The deleted oracle was one literal GLP-38 register path used for all four seats."""
    for value in LITERALS:
        assert "operator-note-closeout-register" not in value
        assert not re.search(r"glp-?\d+-\d{8}", value, re.I), f"issue key in code: {value!r}"
    # the oracle must come from the seat's own brief / config, never a constant path
    assert SA.RESULT_PATH_RE.pattern
    assert "read_brief" in CODE and "result_contract" in CODE


def test_decision_is_invariant_to_last_dispatch_timestamp():
    """The one timestamp in state is a record, never a gate."""
    base = {"last_brief_digest": OLD, "last_result_sig": "absent"}
    outcomes = {SA.decide(dict(base, last_dispatch=t), False, False, brief())["action"]
                for t in (0, 1, 1e9, 1.789e9, 9e18)}
    assert outcomes == {"rearm"}


# ---------------------------------------------------------------- liveness / no deadlock

def _reachable_states():
    """Every combination of persisted values a seat can actually carry."""
    for digest, sig, alerted, rearm, ts in itertools.product(
            (None, OLD, NEW, "other"),                       # last_brief_digest
            (None, "absent", "unreadable", "abc123", OLD),   # last_result_sig
            (None, OLD, NEW, "other"),                       # needs_work_alerted
            (None, OLD, NEW, "other"),                       # rearm_for_digest
            (0, 1.789e9, 9e18)):                             # last_dispatch
        yield {k: v for k, v in (("last_brief_digest", digest), ("last_result_sig", sig),
                                 ("needs_work_alerted", alerted), ("rearm_for_digest", rearm),
                                 ("last_dispatch", ts)) if v is not None}


def test_new_brief_always_wins_from_every_state():
    """No persisted value may suppress newly cut work. This is the anti-deadlock invariant."""
    checked = 0
    for st in _reachable_states():
        if st.get("last_brief_digest") == NEW:
            continue
        for sig, status in (("absent", None), ("abc123", "COMPLETE"), ("zzz", "BLOCKED"),
                            ("unreadable", None)):
            d = SA.decide(dict(st), False, False, brief(digest=NEW, sig=sig, status=status))
            assert d["action"] == "dispatch", (st, sig, status, d)
            checked += 1
    assert checked > 500


def test_decide_is_total():
    """Every input combination lands in exactly one known action - no silent fall-through."""
    actions = set()
    for st in _reachable_states():
        for prev, busy in itertools.product((None, True, False), (None, True, False)):
            for b in (brief(), brief(digest=NEW), brief(sig="abc", status="COMPLETE"),
                      brief(result_path=None), brief(ok=False, reason="parked (brief=null)")):
                d = SA.decide(dict(st), prev, busy, b)
                assert set(d) == {"action", "reason", "progress"}
                assert isinstance(d["reason"], str) and d["reason"]
                actions.add(d["action"])
    assert actions == {"dispatch", "rearm", "needs_new_work", "stalled", "skip"}


def _tick(st, b):
    """One cron tick against a seat that is idle and never writes a RESULT."""
    d = SA.decide(st, False, False, b)
    if d["action"] in ("dispatch", "rearm"):
        st["last_dispatch"] = 1.0
        st["last_brief_digest"] = b["digest"]
        st["last_result_sig"] = b["result"]["sig"]
        st.pop("needs_work_alerted", None)
        if d["action"] == "rearm":
            st["rearm_for_digest"] = b["digest"]
        else:
            st.pop("rearm_for_digest", None)
    return d["action"]


def test_frozen_brief_sends_are_bounded_and_never_grow():
    """The old ratchet made the wait grow forever; the replacement converges to a fixed state."""
    st, b = {}, brief(digest=OLD)
    actions = [_tick(st, b) for _ in range(1000)]
    assert actions.count("dispatch") == 1          # first delivery
    assert actions.count("rearm") == 1             # exactly one re-arm, ever
    assert set(actions[2:]) == {"stalled"}         # then a fixed point, not a growing wait


@pytest.mark.parametrize("k", [1, 2, 3, 5, 17, 99, 500, 999])
def test_a_new_brief_dispatches_on_the_very_next_tick_no_matter_how_long_it_was_stalled(k):
    """Replay of the exact deadlock scenario: after k no-progress ticks, new work still lands."""
    st, old = {}, brief(digest=OLD)
    for _ in range(k):
        _tick(st, old)
    assert SA.decide(st, False, False, brief(digest=NEW))["action"] == "dispatch"


# ---------------------------------------------------------------- per-seat progress oracle

def test_progress_is_judged_against_the_briefs_own_result_path():
    st = {"last_brief_digest": OLD, "last_result_sig": "absent"}
    # seat wrote its own RESULT -> needs new work, no resend
    d = SA.decide(dict(st), False, False, brief(sig="fresh1", status="COMPLETE"))
    assert d["action"] == "needs_new_work" and RESULT_PATH in d["progress"]
    # a DIFFERENT seat's artifact changing must not move this seat: same seat state, its own
    # artifact untouched -> still no progress.
    d2 = SA.decide(dict(st), False, False, brief(sig="absent"))
    assert d2["action"] == "rearm"


def test_previous_briefs_terminal_result_does_not_mask_a_stall():
    """Baseline captured at dispatch; an unchanged terminal RESULT is last brief's, not this one."""
    st = {"last_brief_digest": OLD, "last_result_sig": "sameSig"}
    d = SA.decide(st, False, False, brief(sig="sameSig", status="COMPLETE"))
    assert d["action"] == "rearm"


def test_missing_result_contract_fails_closed_without_a_resend():
    st = {"last_brief_digest": OLD, "last_result_sig": None}
    d = SA.decide(st, False, False, brief(result_path=None))
    assert d["action"] == "needs_new_work" and "no RESULT path" in d["progress"]


def test_result_contract_parses_both_registered_brief_forms():
    assert SA.result_contract("DONE: write /a/b/RESULT.json\n") == "/a/b/RESULT.json"
    assert SA.result_contract("RESULT: /c/d/RESULT.json\n") == "/c/d/RESULT.json"
    assert SA.result_contract("no contract here\n") is None


def test_read_brief_resolves_the_oracle_from_the_seats_own_brief(tmp_path):
    res = tmp_path / "RESULT.json"
    res.write_text(json.dumps({"status": "COMPLETE"}))
    b = tmp_path / "brief.md"
    b.write_text(f"LINEAR: GLP-9\nRESULT: {res}\n")
    out = SA.read_brief({"brief": str(b)})
    assert out["ok"] and out["result"]["path"] == str(res)
    assert out["result"]["status"] == "COMPLETE" and len(out["result"]["sig"]) == 16
    assert SA.read_brief({"brief": None})["reason"].startswith("parked")
    assert "missing" in SA.read_brief({"brief": str(tmp_path / "nope.md")})["reason"]


def test_superseded_brief_is_refused(tmp_path):
    b = tmp_path / "old.md"
    b.write_text("LINEAR: GLP-9\n> SUPERSEDED-BY: /abs/new.md\nRESULT: /x/RESULT.json\n")
    out = SA.read_brief({"brief": str(b)})
    assert out["ok"] is False and "/abs/new.md" in out["reason"]
    assert SA.decide({}, False, False, out)["action"] == "skip"


# ---------------------------------------------------------------- never pages the operator

def test_exhausted_seat_is_logged_never_paged():
    """A seat finishing its brief is normal operation; "no status pings" is a standing order."""
    lowered = CODE.lower()
    for banned in ("pushover", "raise_alert", "red_alert", "notify_operator", "notify_needs_work",
                   "discord", "urlopen", "requests"):
        assert banned not in lowered, f"seat dispatcher must not page the operator: {banned}"
    for value in LITERALS:
        assert "pushover" not in value.lower() and "api.pushover.net" not in value


# ---------------------------------------------------------------- crash-safety of the loop

def _wire(tmp_path, monkeypatch, brief_body, state=None, rc=3):
    cfg = tmp_path / "cfg.json"
    b = tmp_path / "brief.md"
    b.write_text(brief_body)
    cfg.write_text(json.dumps({"seats": {"s1": {"pane": "%1", "tmux": "$1", "session": "z",
                                                "lane": "lane:kael", "issue": "GLP-38",
                                                "brief": str(b)}}}))
    st = tmp_path / "state.json"
    if state is not None:
        st.write_text(json.dumps(state))
    monkeypatch.setattr(SA, "CONFIG", cfg)
    monkeypatch.setattr(SA, "STATE", st)
    monkeypatch.setattr(SA, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(SA, "EVENTS", tmp_path / "orchestrator-events.jsonl", raising=False)
    monkeypatch.setattr(SA, "LOG", tmp_path / "log.jsonl")
    monkeypatch.setattr(SA, "RUNLOCK", tmp_path / "run.lock")
    monkeypatch.setattr(SA, "CAPACITY", tmp_path / "capacity.json")   # no live collector in a unit world
    monkeypatch.setattr(SA, "pane_busy", lambda pane: False)
    monkeypatch.setattr(SA, "observe_busy_route", lambda pane: None)
    monkeypatch.setattr(SA, "current_pane_session", lambda name, seat, seats: seat["session"])
    monkeypatch.setattr(SA.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    sent = []

    class Done:
        returncode = rc
        stdout = "SUBMITTED_UNCONFIRMED"
        stderr = ""

    def fake_run(cmd, **kw):
        sent.append(cmd)
        return Done()

    monkeypatch.setattr(SA.subprocess, "run", fake_run)
    return b, st, sent


def test_state_and_ledger_are_written_the_instant_a_send_is_recorded(tmp_path, monkeypatch):
    b, st, sent = _wire(tmp_path, monkeypatch,
                        "LINEAR: GLP-38\nRESULT: %s/RESULT.json\n" % tmp_path,
                        state={"s1": {"last_pane_busy": False}})
    assert SA._run() == 0
    assert len(sent) == 1 and str(b) in sent[0]
    saved = json.loads(st.read_text())["s1"]
    assert saved["last_brief_digest"] and saved["last_result_sig"] == "absent"
    row = json.loads((tmp_path / "ledger.jsonl").read_text().strip())
    assert row["lane"] == "lane:kael" and row["seat"] == "s1" and row["issue"] == "GLP-38"
    # second run: same brief, no RESULT written -> exactly one re-arm, then no more sends
    SA._run()
    assert len(sent) == 2
    for _ in range(5):
        SA._run()
    assert len(sent) == 2


def test_busy_confirmed_wrapper_result_records_dispatch(tmp_path, monkeypatch):
    b, st, sent = _wire(tmp_path, monkeypatch, "LINEAR: GLP-38\nRESULT: /x/RESULT.json\n",
                        state={"s1": {"last_pane_busy": False}}, rc=0)
    assert SA._run() == 0
    assert sent == [["python3", str(SA.BUSINESS / "scripts/seat_send.py"), "s1", str(b)]]
    assert json.loads(st.read_text())["s1"]["last_brief_digest"]
    assert (tmp_path / "ledger.jsonl").exists()


def test_a_refused_transport_records_no_send(tmp_path, monkeypatch):
    b, st, sent = _wire(tmp_path, monkeypatch, "LINEAR: GLP-38\nRESULT: /x/RESULT.json\n",
                        state={"s1": {"last_pane_busy": False}}, rc=2)
    SA._run()
    assert len(sent) == 1
    assert "last_brief_digest" not in json.loads(st.read_text())["s1"]
    assert not (tmp_path / "ledger.jsonl").exists()


def test_first_ever_sample_never_dispatches(tmp_path, monkeypatch):
    _, st, sent = _wire(tmp_path, monkeypatch, "LINEAR: GLP-38\nRESULT: /x/RESULT.json\n")
    SA._run()
    assert sent == []                      # idle must be sustained across two runs
    SA._run()
    assert len(sent) == 1


def test_a_second_run_will_not_double_send(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, "LINEAR: GLP-38\nRESULT: /x/RESULT.json\n")
    held = open(tmp_path / "run.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert SA.main() == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_corrupt_state_file_does_not_crash_the_run(tmp_path, monkeypatch):
    _, st, sent = _wire(tmp_path, monkeypatch, "LINEAR: GLP-38\nRESULT: /x/RESULT.json\n")
    st.write_text('{"s1": "not-a-dict", "s2": 7}')
    assert SA._run() == 0


def _capacity(tmp_path, status, collected="2099-01-01T00:00:00Z"):
    import json as _json
    p = tmp_path / "capacity.json"
    p.write_text(_json.dumps({"collected_at_utc": collected, "records": [
        {"provider": "openai-codex", "label": f"codex-{i}", "capacity_status": status,
         "weekly_reset_at_utc": "2026-09-19T08:12:51Z"} for i in range(3)]}))
    return p


def test_depleted_provider_parks_the_seat_with_a_reason(tmp_path, monkeypatch):
    """2026-09-16: every codex account hit 0%, seats returned 429, and this dispatcher kept sending."""
    monkeypatch.setattr(SA, "CAPACITY", _capacity(tmp_path, "depleted"))
    brief = tmp_path / "b.md"; brief.write_text("LINEAR: GLP-1\nDONE: write /abs/RESULT.json\n")
    r = SA.read_brief({"brief": str(brief)})
    assert r["ok"] is False and "depleted" in r["reason"] and "429" in r["reason"]


def test_partial_or_stale_capacity_never_silences_dispatch(tmp_path, monkeypatch):
    """One live account is enough to send; an old collector snapshot is unknown, not depleted."""
    brief = tmp_path / "b.md"; brief.write_text("LINEAR: GLP-1\nDONE: write /abs/RESULT.json\n")
    monkeypatch.setattr(SA, "CAPACITY", _capacity(tmp_path, "limited"))
    assert SA.read_brief({"brief": str(brief)})["ok"] is True
    monkeypatch.setattr(SA, "CAPACITY", _capacity(tmp_path, "depleted", collected="2020-01-01T00:00:00Z"))
    assert SA.read_brief({"brief": str(brief)})["ok"] is True
    monkeypatch.setattr(SA, "CAPACITY", tmp_path / "absent.json")
    assert SA.read_brief({"brief": str(brief)})["ok"] is True


def _events(tmp_path):
    p = tmp_path / "orchestrator-events.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_brief_and_native_status_route_parsing_ignores_prose():
    body = ("LINEAR: GLP-43\nMODEL: gpt-6-astra-900k\nPROVIDER: openai-codex\n"
            "REASONING: high; verify effective runtime setting\n"
            "Use gpt-6-sol medium for a named child only.\n")
    assert brief_route(body) == ("gpt-6-astra-900k", "high")
    instruction = route_instruction(*brief_route(body))
    assert "/model gpt-6-astra-900k" in instruction
    assert "/reasoning high" in instruction and "BEFORE starting" in instruction
    assert "/status" not in instruction
    assert "readback is sufficient" in instruction
    assert brief_route("REASONING: high\nNo MODEL field\n") is None
    screen = ("Prior conversation says gpt 6 astra 900k high\n"
              " ─ (⌐■_■) musing… · 21m │ gpt 6 sol high │ 178.9k/256k │ voice off\n"
              "Ctrl+C to interrupt\n")
    assert status_route(screen) == ("gpt-6-sol", "high")
    assert status_route(" ─ ready │ gpt 6 astra 900k │ 161k/872k │ 1 session\n") == ("gpt-6-astra-900k", None)
    assert status_route("The response says gpt 6 sol medium\n") is None


def test_busy_route_observation_uses_fake_tmux_only(monkeypatch):
    calls = []
    def fake_capture(cmd, **kwargs):
        calls.append(cmd)
        return type("Capture", (), {"returncode": 0,
            "stdout": " ─ ( ˘⌣˘)♡ ruminating… │ gpt 6 sol medium │ 2k/256k │ voice off\n"})()
    monkeypatch.setattr(SA.subprocess, "run", fake_capture)
    assert SA.observe_busy_route("%1") == ("gpt-6-sol", "medium")
    assert calls == [["tmux", "capture-pane", "-p", "-t", "%1", "-S", "-5"]]


def test_busy_mismatch_event_is_once_per_brief_even_after_state_loss(tmp_path, monkeypatch):
    body = ("LINEAR: GLP-43\nMODEL: gpt-6-astra-900k\nPROVIDER: openai-codex\nREASONING: high; verify\n"
            f"RESULT: {tmp_path}/RESULT.json\n")
    b, st, sent = _wire(tmp_path, monkeypatch, body, state={"s1": {"last_pane_busy": True}})
    monkeypatch.setattr(SA, "pane_busy", lambda pane: True)
    observed = ("gpt-6-sol", "medium")
    monkeypatch.setattr(SA, "observe_busy_route", lambda pane: observed)
    assert SA._run() == 0
    assert sent == []
    rows = _events(tmp_path)
    assert len(rows) == 1 and rows[0]["kind"] == "seat_model_mismatch"
    assert rows[0]["expected_model"] == "gpt-6-astra-900k"
    assert rows[0]["expected_effort"] == "high"
    assert rows[0]["observed_model"] == "gpt-6-sol"
    assert rows[0]["observed_effort"] == "medium"
    for _ in range(3):
        assert SA._run() == 0
    saved = json.loads(st.read_text())
    saved["s1"].pop("last_event_keys", None)
    st.write_text(json.dumps(saved))
    assert SA._run() == 0 and len(_events(tmp_path)) == 1
    b.write_text(body + "# revised assignment\n")
    assert SA._run() == 0 and len(_events(tmp_path)) == 2
    assert _events(tmp_path)[0]["brief_digest"] != _events(tmp_path)[1]["brief_digest"]
    assert sent == []
    observed = ("gpt-6-astra-900k", "high")
    b.write_text(body + "# next revision\n")
    assert SA._run() == 0 and len(_events(tmp_path)) == 2


def test_effort_only_mismatch_and_unknown_effort_are_handled(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text("MODEL: gpt-6-astra-900k\nPROVIDER: openai-codex\nREASONING: high\n")
    seat = {"brief": str(path)}
    assert SA.busy_model_mismatch(seat, True, ("gpt-6-astra-900k", "medium"))["observed_effort"] == "medium"
    assert SA.busy_model_mismatch(seat, True, ("gpt-6-astra-900k", None)) is None
    assert SA.busy_model_mismatch(seat, True, ("gpt-6-sol", None))["observed_effort"] == "unknown"
    assert SA.busy_model_mismatch(seat, True, ("gpt-6-astra-900k", "high")) is None
    assert SA.busy_model_mismatch(seat, False, ("gpt-6-sol", "high")) is None


def test_completion_event_once_per_seat_digest_and_result_path(tmp_path, monkeypatch):
    result = tmp_path / "RESULT.json"
    b, st, sent = _wire(tmp_path, monkeypatch, f"LINEAR: GLP-38\nRESULT: {result}\n",
                        state={"s1": {"last_pane_busy": False}})
    assert SA._run() == 0 and len(sent) == 1
    result.write_text('{"status":"COMPLETE"}')
    for _ in range(6):
        assert SA._run() == 0
    rows = _events(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "seat_needs_work" and row["seat"] == "s1"
    assert row["issue"] == "GLP-38" and row["brief"] == str(b)
    assert row["result_path"] == str(result) and row["brief_digest"]
    assert row["ts"] and "NEW assignment" in row["reason"]
    # A crash after append but before saving the dedupe cursor must not duplicate the event.
    saved = json.loads(st.read_text())
    saved["s1"].pop("last_event_keys", None)
    st.write_text(json.dumps(saved))
    assert SA._run() == 0 and len(_events(tmp_path)) == 1
    # A new brief digest, even with the same result path, is independently reportable.
    b.write_text(b.read_text() + "# revision\n")
    assert SA._run() == 0 and len(sent) == 2
    result.write_text('{"status":"BLOCKED"}')
    assert SA._run() == 0
    assert len(_events(tmp_path)) == 2
    assert _events(tmp_path)[0]["brief_digest"] != _events(tmp_path)[1]["brief_digest"]


def test_amended_refused_brief_still_reports_its_own_completion_once(tmp_path, monkeypatch):
    """A clause-gate refusal must not hide a RESULT for the brief actually sent."""
    result = tmp_path / "RESULT.json"
    body = f"LINEAR: GLP-38\nRESULT: {result}\n"
    b, st, sent = _wire(tmp_path, monkeypatch, body,
                        state={"s1": {"last_pane_busy": False}})
    assert SA._run() == 0 and len(sent) == 1
    dispatched_digest = json.loads(st.read_text())["s1"]["last_brief_digest"]
    b.write_text(body + "# short amendment; old brief fails clause admission\n")

    def refuse(cmd, **kwargs):
        sent.append(cmd)
        return type("Refusal", (), {"returncode": 2,
            "stdout": "REFUSED: brief refused by scripts/brief_clause_gate.py", "stderr": ""})()
    monkeypatch.setattr(SA.subprocess, "run", refuse)
    assert SA._run() == 0 and len(sent) == 2
    assert "brief refused by scripts/brief_clause_gate.py" in json.loads(
        (tmp_path / "log.jsonl").read_text().splitlines()[-1])["skipped"]["s1"]
    assert _events(tmp_path) == []  # a refusal is never a dispatch or completion
    result.write_text(json.dumps({"issue": "GLP-38", "brief": str(b), "status": "BLOCKED"}))
    assert SA._run() == 0
    assert len(sent) == 2  # completed result bypasses only observation, not a send gate
    rows = _events(tmp_path)
    assert len(rows) == 1 and rows[0]["kind"] == "seat_needs_work"
    assert rows[0]["result_sig"] == SA.result_state(str(result))["sig"]
    assert rows[0]["brief_digest"] == dispatched_digest
    assert SA._run() == 0 and len(_events(tmp_path)) == 1 and len(sent) == 2
    result.write_text(json.dumps({"issue": "GLP-38", "brief": str(b),
                                  "status": "COMPLETE", "revision": 2}))
    assert SA._run() == 0 and len(_events(tmp_path)) == 2 and len(sent) == 2
    assert _events(tmp_path)[-1]["result_sig"] == SA.result_state(str(result))["sig"]


def test_amended_brief_does_not_report_stale_or_foreign_result(tmp_path, monkeypatch):
    result = tmp_path / "RESULT.json"
    body = f"LINEAR: GLP-38\nRESULT: {result}\n"
    b, st, sent = _wire(tmp_path, monkeypatch, body,
                        state={"s1": {"last_pane_busy": False}})
    assert SA._run() == 0 and len(sent) == 1
    # The previous result was present when the pointer landed: terminal is not new work.
    result.write_text(json.dumps({"issue": "GLP-38", "brief": str(b), "status": "COMPLETE"}))
    assert SA._run() == 0 and len(_events(tmp_path)) == 1  # changed since dispatch
    # Reassigning the pointer must not attribute a further rewrite of that old result to the new brief.
    other = tmp_path / "other.md"
    other.write_text(body)
    cfg = tmp_path / "cfg.json"
    config = json.loads(cfg.read_text())
    config["seats"]["s1"]["brief"] = str(other)
    cfg.write_text(json.dumps(config))
    result.write_text(json.dumps({"issue": "GLP-38", "brief": str(b),
                                  "status": "BLOCKED", "revision": 2}))
    assert SA._run() == 0 and len(_events(tmp_path)) == 1
    # An amended pointer is also not evidence when the result names another brief or issue.
    config["seats"]["s1"]["brief"] = str(b)
    cfg.write_text(json.dumps(config))
    b.write_text(body + "# amendment\n")
    result.write_text(json.dumps({"issue": "GLP-72", "brief": str(b),
                                  "status": "COMPLETE", "revision": 3}))
    assert SA._run() == 0 and len(_events(tmp_path)) == 1
    assert len([c for c in sent if _is_send(c)]) >= 2  # guard may refuse; no false completion


def test_amended_brief_requires_fresh_result_and_original_contract(tmp_path, monkeypatch):
    result = tmp_path / "RESULT.json"
    body = f"LINEAR: GLP-38\nRESULT: {result}\n"
    b, st, sent = _wire(tmp_path, monkeypatch, body,
                        state={"s1": {"last_pane_busy": False}})
    result.write_text(json.dumps({"issue": "GLP-38", "brief": str(b), "status": "COMPLETE"}))
    assert SA._run() == 0 and len(sent) == 1
    original = json.loads(st.read_text())["s1"]
    assert original["last_result_sig"] == SA.result_state(str(result))["sig"]
    def refuse(cmd, **kwargs):
        sent.append(cmd)
        return type("Refusal", (), {"returncode": 2,
            "stdout": "REFUSED: brief refused by scripts/brief_clause_gate.py", "stderr": ""})()
    monkeypatch.setattr(SA.subprocess, "run", refuse)
    b.write_text(body + "# amended old instruction\n")
    # Unchanged terminal output predates the send; it must not become completion.
    assert SA._run() == 0 and _events(tmp_path) == []
    changed = tmp_path / "other" / "RESULT.json"
    changed.parent.mkdir()
    changed.write_text(json.dumps({"issue": "GLP-38", "brief": str(b),
                                   "status": "BLOCKED", "revision": 2}))
    b.write_text(f"LINEAR: GLP-38\nRESULT: {changed}\n# amended result contract\n")
    assert SA._run() == 0 and _events(tmp_path) == []
    assert json.loads(st.read_text())["s1"]["last_result_path"] == str(result)


def test_rewritten_result_same_brief_emits_new_completion_event(tmp_path, monkeypatch):
    result = tmp_path / "RESULT.json"
    b, st, sent = _wire(tmp_path, monkeypatch, f"LINEAR: GLP-38\nRESULT: {result}\n",
                        state={"s1": {"last_pane_busy": False}})
    assert SA._run() == 0 and len(sent) == 1
    result.write_text('{"status":"BLOCKED","revision":1}')
    assert SA._run() == 0
    first = _events(tmp_path)
    assert len(first) == 1 and first[0]["kind"] == "seat_needs_work"
    assert SA._run() == 0 and _events(tmp_path) == first  # unchanged bytes: no duplicate

    result.write_text('{"status":"BLOCKED","revision":2}')
    assert SA._run() == 0
    rows = _events(tmp_path)
    assert len(rows) == 2
    assert all(r["brief"] == str(b) and r["result_path"] == str(result) for r in rows)
    assert rows[0]["brief_digest"] == rows[1]["brief_digest"]
    assert rows[0]["event_key"] != rows[1]["event_key"]
    assert SA._run() == 0 and _events(tmp_path) == rows  # new bytes also emit only once
    assert len(sent) == 1  # never re-send a completed brief


def _is_send(cmd):
    return any(str(part).endswith('/scripts/seat_send.py') for part in cmd)


def test_session_mismatch_is_recorded_once_and_rearmed_only_on_address_change(tmp_path, monkeypatch):
    b, st, _ = _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
                      state={"s1": {"last_pane_busy": False}})
    sent = []
    class Refusal:
        returncode = 2
        stdout = "REFUSED: actual Hermes session mismatch: live-session"
        stderr = ""
    def refuse(cmd, **kwargs):
        sent.append(cmd)
        return Refusal()
    monkeypatch.setattr(SA.subprocess, "run", refuse)
    assert SA._run() == 0 and len(sent) == 1
    saved = json.loads(st.read_text())["s1"]
    assert saved["invalid_address"]["pane"] == "%1"
    assert saved["invalid_address"]["tmux"] == "$1"
    assert saved["invalid_address"]["session"] == "z"
    assert len(_events(tmp_path)) == 1
    assert _events(tmp_path)[0]["kind"] == "seat_address_invalid"
    assert _events(tmp_path)[0]["seat"] == "s1"
    assert "actual Hermes session mismatch" in _events(tmp_path)[0]["reason"]
    for _ in range(5):
        assert SA._run() == 0
    # New brief bytes alone cannot cure an observed wrong destination.
    b.write_text(b.read_text() + "# different assignment\n")
    assert SA._run() == 0
    assert len([c for c in sent if _is_send(c)]) == 1 and len(_events(tmp_path)) == 1
    assert len([c for c in sent if '--check' in c]) == 6
    assert "last_brief_digest" not in json.loads(st.read_text())["s1"]
    cfg = tmp_path / "cfg.json"
    data = json.loads(cfg.read_text())
    data["seats"]["s1"]["session"] = "corrected-session"
    cfg.write_text(json.dumps(data))
    assert SA._run() == 0
    assert len([c for c in sent if _is_send(c)]) == 2
    assert [c for c in sent if _is_send(c)][-1][2] == "s1"
    assert json.loads(st.read_text())["s1"]["invalid_address"]["session"] == "corrected-session"
    assert len(_events(tmp_path)) == 2
    assert all(e["kind"] == "seat_address_invalid" for e in _events(tmp_path))
    assert SA._run() == 0 and len([c for c in sent if _is_send(c)]) == 2

def test_new_session_recovers_from_native_pane_identity_without_live_send(tmp_path, monkeypatch):
    resolver = SA.current_pane_session
    _, state, _ = _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
                        state={"s1": {"last_pane_busy": False,
                                      "invalid_address": {"pane": "%1", "tmux": "$1", "session": "z"}}})
    monkeypatch.setattr(SA, "current_pane_session", resolver)
    proc = tmp_path / "proc"
    monkeypatch.setattr(SA, "PROC", proc)
    for pid in (10, 11):
        (proc / str(pid) / "task" / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "task" / str(pid) / "children").write_text("11" if pid == 10 else "")
    (proc / "10" / "environ").write_bytes(b"PANE=1\0")
    native = tmp_path / "native-session.json"
    (proc / "11" / "environ").write_bytes(
        b"HERMES_TUI_ACTIVE_SESSION_FILE=" + str(native).encode() + b"\0")
    sid = "20260924_021218_29e871"
    calls = []
    wrong_pane = True

    class Done:
        def __init__(self, code, output):
            self.returncode, self.stdout, self.stderr = code, output, ""

    def transport(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["tmux", "display-message"]:
            return Done(0, f"{'%2' if wrong_pane else '%1'}|$1|10|0\n")
        return Done(0 if "--check" in cmd else 3, "SUBMITTED_UNCONFIRMED")

    monkeypatch.setattr(SA.subprocess, "run", transport)
    assert SA._run() == 0
    assert "unprovable" in json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])["skipped"]["s1"]
    assert not any(_is_send(call) for call in calls)
    wrong_pane = False
    assert SA._run() == 0  # the native file is still unreadable
    assert not any(_is_send(call) for call in calls)
    native.write_text(json.dumps({"session_id": sid}))  # /new has updated the TUI-owned file
    assert SA._run() == 0
    saved = json.loads((tmp_path / "cfg.json").read_text())["seats"]["s1"]
    assert saved["session"] == sid
    assert "invalid_address" not in json.loads(state.read_text())["s1"]
    assert len([call for call in calls if "--check" in call]) == 1
    sends = [call for call in calls if _is_send(call)]
    assert len(sends) == 1 and sends[0][2] == 's1'
    log = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert log["session_recovered"] == [{"seat": "s1", "pane": "%1", "tmux": "$1",
                                         "previous": "z", "session": sid,
                                         "verified_by": "tui_send.sh --check"}]
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1


def test_same_address_rearms_only_after_read_only_check_succeeds(tmp_path, monkeypatch):
    _, st, _ = _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
                     state={"s1": {"last_pane_busy": False}})
    calls = []
    healthy = False

    def transport(cmd, **kwargs):
        calls.append(cmd)
        if '--check' in cmd:
            return SimpleResult(0 if healthy else 2, 'REFUSED: pane/session mismatch or dead pane')
        if len([c for c in calls if _is_send(c)]) == 1:
            return SimpleResult(2, 'REFUSED: actual Hermes session mismatch: wrong')
        return SimpleResult(3, 'SUBMITTED_UNCONFIRMED')

    class SimpleResult:
        def __init__(self, code, output):
            self.returncode, self.stdout, self.stderr = code, output, ''

    monkeypatch.setattr(SA.subprocess, 'run', transport)
    assert SA._run() == 0
    assert len(_events(tmp_path)) == 1
    assert SA._run() == 0
    assert '--check' in calls[-1] and calls[-1][3:] == ['%1', '$1', 'z']
    assert len([c for c in calls if _is_send(c)]) == 1
    assert json.loads(st.read_text())['s1']['invalid_address']['session'] == 'z'
    healthy = True  # the live pane recovers without changing any configured address byte
    assert SA._run() == 0
    assert len([c for c in calls if _is_send(c)]) == 2
    assert calls[-2][2] == '--check' and _is_send(calls[-1])
    assert 'invalid_address' not in json.loads(st.read_text())['s1']
    assert [e['kind'] for e in _events(tmp_path)] == ['seat_address_invalid', 'seat_address_valid']
    assert SA._run() == 0
    assert len([e for e in _events(tmp_path) if e['kind'] == 'seat_address_valid']) == 1


def test_recovery_event_failure_does_not_clear_latch_or_send(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
          state={'s1': {'last_pane_busy': False,
                        'invalid_address': {'pane': '%1', 'tmux': '$1', 'session': 'z'}}})
    calls = []
    def check(cmd, **kwargs):
        calls.append(cmd)
        return type('Check', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})()
    monkeypatch.setattr(SA.subprocess, 'run', check)
    monkeypatch.setattr(SA, 'event_append_once', lambda *_: 'fixture write failure')
    assert SA._run() == 0
    assert len(calls) == 1 and '--check' in calls[0]
    assert json.loads((tmp_path / 'state.json').read_text())['s1']['invalid_address']['pane'] == '%1'


def test_address_event_recovers_from_append_before_state_and_pane_change(tmp_path, monkeypatch):
    _, st, _ = _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
                     state={"s1": {"last_pane_busy": False}})
    calls = []
    class Refusal:
        returncode = 2
        stdout = "REFUSED: pane/session mismatch or dead pane"
        stderr = ""
    def refuse(cmd, **kwargs):
        calls.append(cmd)
        return Refusal()
    monkeypatch.setattr(SA.subprocess, "run", refuse)
    SA._run()
    assert len(_events(tmp_path)) == 1
    saved = json.loads(st.read_text())
    saved["s1"].pop("invalid_address")
    saved["s1"].pop("last_event_keys")
    st.write_text(json.dumps(saved))
    SA._run()  # simulate append landed, state write lost: ledger must dedupe it
    assert len([c for c in calls if _is_send(c)]) == 2 and len(_events(tmp_path)) == 1
    SA._run()
    assert len([c for c in calls if _is_send(c)]) == 2
    cfg = tmp_path / "cfg.json"
    data = json.loads(cfg.read_text())
    data["seats"]["s1"]["pane"] = "%2"
    cfg.write_text(json.dumps(data))
    SA._run()
    assert len([c for c in calls if _is_send(c)]) == 3 and calls[-1][2] == "s1"
    assert len(_events(tmp_path)) == 2


def test_non_session_refusal_busy_park_and_depleted_guards_unchanged(tmp_path, monkeypatch):
    b, st, sent = _wire(tmp_path, monkeypatch, f"RESULT: {tmp_path}/RESULT.json\n",
                         state={"s1": {"last_pane_busy": False}}, rc=2)
    assert SA._run() == 0 and len(sent) == 1
    assert "invalid_address" not in json.loads(st.read_text())["s1"]
    assert SA._run() == 0 and len(sent) == 2  # ordinary refusal is not latched
    monkeypatch.setattr(SA, "pane_busy", lambda pane: True)
    assert SA._run() == 0 and len(sent) == 2
    monkeypatch.setattr(SA, "pane_busy", lambda pane: False)
    cfg = tmp_path / "cfg.json"
    data = json.loads(cfg.read_text())
    data["seats"]["s1"]["brief"] = None
    cfg.write_text(json.dumps(data))
    assert SA._run() == 0 and len(sent) == 2
    data["seats"]["s1"]["brief"] = str(b)
    cfg.write_text(json.dumps(data))
    _capacity(tmp_path, "depleted")
    assert SA._run() == 0 and len(sent) == 2
    assert _events(tmp_path) == []
