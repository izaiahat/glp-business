"""Offline unchanged-AST caller tests; no host imports, transports or live state.

Fixture fields are verbatim diagnostic/accounting fields from the three source
receipts, identified by row and SHA. Runtime authority and clean-room services
are fixture-bound; this is caller coverage, not installed-runtime validation.
"""
import ast
from copy import deepcopy
import datetime as dt
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/glp186_binding_refusals.json").read_text())
NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc)


def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    # Literal configuration only; never run module initialization or imports.
    for n in tree.body:
        if isinstance(n, ast.Assign):
            try:
                value = ast.literal_eval(n.value)
            except (ValueError, TypeError):
                continue
            for target in n.targets:
                if isinstance(target, ast.Name):
                    namespace.setdefault(target.id, value)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def ad(tmp_path):
    ns = dict(Any=Any, Callable=Callable, Path=Path, dt=dt, json=json, os=os, re=re,
              ENABLED=True, MAX_CYCLES_PER_DAY=3, MAX_IN_FLIGHT=3, BUDGET_S=2700,
              HTTP_CONNECT_TIMEOUT_S=10, HTTP_READ_TIMEOUT_S=30,
              HTTP_DEADLINE_S=60, HTTP_MAX_ATTEMPTS=3,
              PRIMARY_ENGINE="fixture", PRIMARY_MODEL="fixture-model", PRIMARY_REASONING="high",
              MODEL="fixture-model", MAX_TURNS=20, RESEARCH_MAX_TURNS=80, BRIDGE_MAX_TURNS=80,
              RESEARCH_PACKET_CLASS=frozenset({"program_authority"}),
              IN_FLIGHT="in_flight", RESOLVED="resolved", FAILED="failed", REFUSED="refused",
              DISPATCH_FAILED="dispatch_failed", CAPABILITY="capability", EVIDENCE_GAP="evidence_gap",
              NO_FAILURE="", STATE_PATH=tmp_path / "ledger.json",
              pt_day=lambda now=None: "2026-09-26", operator_override_at=lambda *a: "",
              provider_identity_gate=lambda *a: {"ok": True},
              authoring_blocks=lambda rows, provider: rows,
              non_authoring_blocks=lambda *a: [], cycle_fetch_receipts=lambda *a: [],
              reused_prior_artifacts=lambda *a: [],
              fetch_receipts=SimpleNamespace(receipt_rejections=lambda *a: []),
              case_signature=lambda *a: "fixture-signature")
    names = {"classify_failure", "_capability_marker", "_ingest", "provider_record",
             "latest_cycle", "cycles_today", "in_flight_cycles", "engine_for", "primary_engine",
             "dispatch_class", "may_dispatch", "escalation_case", "utc_now", "parse_utc",
             "read_authoring_receipt", "locate_authoring_receipt"}
    load_functions(ROOT / "scripts/glp_onboarding_authoring_dispatch.py", names, ns)
    ns["save_state"] = lambda state, path: Path(path).write_text(json.dumps(state))
    return ns


def ingest_fixture(ad, tmp_path, fixture):
    agent = deepcopy(fixture["receipt"])
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(agent))
    before = path.read_bytes()
    provider = agent["provider"]
    record = dict(provider=provider, cycle=1, pt_day="2026-09-26", status="in_flight",
                  job_id="fixture-job", authoring_receipt=str(path),
                  deadline_utc="2026-09-26T13:00:00Z", blocked_steps=["program_authority"])
    state = {"providers": {provider: {"cycles": [record]}}}
    result = ad["_ingest"](provider, state, NOW,
                           lambda _: {"status": "done", "rc": 0}, ad["STATE_PATH"])
    assert path.read_bytes() == before
    assert json.loads(ad["STATE_PATH"].read_text()) == state
    return agent, record, state, result


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: str(f["row"]))
def test_binding_refusal_survives_ingest_status_and_error_payload(ad, tmp_path, fixture):
    agent, record, state, result = ingest_fixture(ad, tmp_path, fixture)
    assert result["failure_class"] == record["failure_class"] == "capability"
    assert agent["notes"][:200] in record["failure_reason"]
    assert "evidence answer" not in record["failure_reason"]
    ad["load_state"] = lambda: state
    status_ns = load_functions(ROOT / "scripts/glp_onboarding_authoring_status.py",
                               {"summarize"}, {"ad": SimpleNamespace(**ad), "Path": Path})
    view = status_ns["summarize"]()["providers"][0]
    assert view["last_failure_class"] == "capability"
    assert view["agent_verdict"] == "not_authored"
    case = ad["escalation_case"](agent["provider"], {}, state, NOW)
    assert "resolved (capability)" in case["error_text"]
    assert "evidence_gap" not in case["error_text"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: str(f["row"]))
def test_not_authored_and_accepted_receipt_accounting_unchanged(ad, tmp_path, fixture):
    agent, record, state, result = ingest_fixture(ad, tmp_path, fixture)
    assert result["ingested"] is True and result["reason_code"] == "receipt"
    assert record["status"] == "resolved"  # receipt read, NOT authored acceptance
    assert record["agent_verdict"] == result["agent_verdict"] == "not_authored"
    assert record["agent_fit_verdict"] == result["agent_fit_verdict"] == "unknown"
    assert result["steps_addressed"] == []
    assert result["steps_not_addressed"] == agent["steps_not_addressed"]
    # Row 660 cites only its own refusal receipt, not an authored provider artifact.
    assert record["artifacts_written"] == result["artifacts_written"] == agent["artifacts_written"]
    assert len(state["providers"][agent["provider"]]["cycles"]) == 1
    assert not state.get("not_fit")


@pytest.mark.parametrize("count,reason", [(1, "eligible"), (3, "cycle_cap")])
@pytest.mark.parametrize("steps", [[], ["program_authority"]])
def test_class_does_not_change_retry_admission_budget_or_model(ad, count, reason, steps):
    provider = "fixture-provider"
    receipt = {"not_green": [{"step": "program_authority"}]}
    outcomes = []
    for failure_class in ["evidence_gap", "capability"]:
        state = {"providers": {provider: {"cycles": [
            dict(provider=provider, cycle=i + 1, pt_day="2026-09-26", status="resolved",
                 agent_verdict="not_authored", failure_class=failure_class)
            for i in range(count)]}}}
        before = deepcopy(state)
        admission = ad["may_dispatch"](provider, receipt, state, NOW)
        engine = ad["engine_for"](provider, state, NOW, blocked_steps=steps)
        assert admission["reason_code"] == reason
        assert engine["escalated"] is False
        assert engine["max_turns"] == (80 if steps else 20)
        assert len(ad["cycles_today"](state, provider, NOW)) == count
        assert state == before
        outcomes.append((admission, engine))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("declared", ["evidence_gap", ""])
def test_genuine_and_legacy_evidence_gap_still_evidence_gap(ad, tmp_path, declared):
    fixture = {"receipt": dict(provider="fixture", verdict="not_authored", fit_verdict="unknown",
                               failure_class=declared, notes="Official sources omit the required terms.")}
    _, record, _, result = ingest_fixture(ad, tmp_path, fixture)
    assert result["failure_class"] == record["failure_class"] == "evidence_gap"


@pytest.mark.parametrize("record,agent,job,why", [
    ({"dispatch_error": "refused"}, {"failure_class": "evidence_gap"}, {}, "never started"),
    ({}, {"failure_class": "evidence_gap"}, {"rc": 1}, "rc=1"),
    ({}, {"failure_class": "evidence_gap"}, {"status": "timeout"}, "job status"),
    ({}, {"failure_class": "evidence_gap", "notes": "max turns"}, {}, "ceiling"),
    ({}, {}, {"rc": 0}, "mandatory receipt"),
])
def test_objective_runtime_precedence_unchanged(ad, record, agent, job, why):
    result = ad["classify_failure"](record, agent, job)
    assert result["failure_class"] == "capability"
    assert why in result["why"]


def test_clean_room_rejection_still_precedes_declared_capability(ad, tmp_path):
    ad["reused_prior_artifacts"] = lambda *a: ["fixture-old-capture"]
    _, record, _, result = ingest_fixture(ad, tmp_path, FIXTURES[0])
    assert record["status"] == "failed"
    assert record["failure_class"] == "evidence_gap"
    assert result["reason_code"] == "reused_prior_artifacts"
