"""GLP-324: an unassigned commission is refused before producer execution.

Offline unchanged-AST caller tests, same discipline as the GLP-186 suite: the
dispatcher's own function bodies are executed with literal configuration only,
never module initialization or imports; every external service is a stub.
"""
import ast
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/glp_onboarding_authoring_dispatch.py"
NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc)
PROVIDER = "fixture-provider"
RECEIPT = {"receipt_path": "machine-receipt.json",
           "not_green": [{"provider": PROVIDER, "step": "program_authority"}]}
ENGINE = {"engine": "fixture", "model": "fixture-model", "reasoning": "high",
          "why": "fixture", "policy": "fixture-policy", "dispatch_class": "research",
          "max_turns": 20}


def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    functions = [n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    for n in tree.body:
        if isinstance(n, ast.Assign):
            try:
                value = ast.literal_eval(n.value)
            except (ValueError, TypeError):
                continue
            for target in n.targets:
                if isinstance(target, ast.Name):
                    namespace.setdefault(target.id, value)
    # Decorators (ledger_lock's @contextmanager) are resolved from the namespace.
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def ad(tmp_path):
    receipts = tmp_path / "authoring"
    ns = dict(Any=Any, Callable=Callable, Path=Path, dt=dt, json=json, os=os, re=re,
              contextmanager=contextmanager,
              fcntl=SimpleNamespace(flock=lambda *a: None, LOCK_EX=0, LOCK_UN=0),
              ENABLED=True, MAX_CYCLES_PER_DAY=3, MAX_IN_FLIGHT=1, BUDGET_S=2700,
              MAX_TURNS=20, IN_FLIGHT="in_flight", DISPATCH_FAILED="dispatch_failed",
              STATE_PATH=tmp_path / "ledger.json",
              pt_day=lambda now=None: "2026-09-26",
              utc_now=lambda now=None: "2026-09-26T12:00:00Z",
              provider_identity_gate=lambda *a: {"ok": True},
              authoring_blocks=lambda rows, provider: list(rows),
              in_flight_cycles=lambda state: [],
              cycles_today=lambda state, provider, now=None: [],
              receipt_dir=lambda: receipts,
              receipt_path_for=lambda p, c, s="": str(receipts / f"authoring-{s}-{p}-c{c}.json"),
              capture_root_for=lambda p, c, r: str(tmp_path / "capture" / f"{p}-c{c}"),
              prior_fingerprints=lambda p: {},
              build_brief=lambda **kw: "BRIEF for %s c%d" % (kw["provider"], kw["cycle"]),
              sink=lambda: "",
              poster_for=lambda eng: pytest.fail("real transport must never be selected"),
              fetch_receipts=SimpleNamespace(cycle_id_for_capture_root=lambda root: "cycle-id"),
              classify_failure=lambda record: {"failure_class": "capability", "why": "x"},
              persist_machine_receipt=lambda record, reason, now=None: pytest.fail(
                  "no machine receipt is expected in these cases"),
              load_state=lambda path=None: {},
              save_state=lambda state, path=None: Path(path).write_text(json.dumps(state)),
              provider_record=lambda state, p: state.setdefault("providers", {}).setdefault(p, {}),
              dispatch_class=lambda steps=(): "research")
    names = {"commissioning_assignee_gate", "may_dispatch", "_dispatch", "dispatch",
             "ledger_lock"}
    load_functions(SOURCE, names, ns)
    return ns


def commission(assignee):
    return {"issue": {"identifier": "GLP-999", "assignee": assignee},
            "review": {"verdict": "APPROVE"}, "brief_path": "brief.md",
            "brief_sha256": "0" * 64, "tier": "T1", "brief_text": "reviewed brief"}


def forbidden_producer(*args, **kwargs):
    pytest.fail("producer invoked for an unassigned commission")


UNASSIGNED = [None, {}, {"id": None, "name": None}, {"id": "", "name": ""},
              {"id": "   ", "name": "someone"}]


@pytest.mark.parametrize("assignee", UNASSIGNED)
def test_unassigned_commission_refused_before_bridge_producer(ad, tmp_path, assignee):
    state = {}
    state_path = tmp_path / "ledger.json"
    result = ad["_dispatch"](provider=PROVIDER, receipt=RECEIPT, state=state,
                             bridge_post=forbidden_producer, now=NOW,
                             state_path=state_path, fingerprints={}, engine=ENGINE,
                             commissioning=commission(assignee))
    assert result["dispatched"] is False
    assert result["reason_code"] == "commissioning_assignee_unset"
    assert result["reason_code"] == ad["COMMISSIONING_ASSIGNEE_UNSET"]
    assert "GLP-999" in result["why"]
    # No cycle accounted, no ledger write, no receipt claiming authoring.
    assert state == {}
    assert not state_path.exists()
    assert not (tmp_path / "authoring").exists()
    assert "cycle" not in result and "authoring_receipt" not in result


@pytest.mark.parametrize("assignee", UNASSIGNED)
def test_unassigned_commission_refused_before_native_producer(ad, tmp_path, assignee):
    state_path = tmp_path / "ledger.json"
    result = ad["dispatch"](entry="native", native_runner=forbidden_producer,
                            provider=PROVIDER, receipt=RECEIPT, now=NOW,
                            state_path=state_path, fingerprints={}, engine=ENGINE,
                            commissioning=commission(assignee))
    assert result["dispatched"] is False
    assert result["reason_code"] == "commissioning_assignee_unset"
    assert not state_path.exists()
    assert not (tmp_path / "authoring").exists()


def test_other_guards_still_take_precedence(ad, tmp_path):
    ad["ENABLED"] = False
    result = ad["_dispatch"](provider=PROVIDER, receipt=RECEIPT, state={},
                             bridge_post=forbidden_producer, now=NOW,
                             state_path=tmp_path / "ledger.json", fingerprints={},
                             engine=ENGINE, commissioning=commission(None))
    assert result["reason_code"] == "disabled"


@pytest.mark.parametrize("commissioning", [commission({"id": "user-1", "name": "Worker"}), None])
def test_assigned_or_uncommissioned_dispatch_proceeds_unchanged(ad, tmp_path, commissioning):
    calls = []

    def producer(brief, model=None, timeout_s=None, callback_url=""):
        calls.append((brief, model, timeout_s, callback_url))
        return {"job_id": "job-1", "status": "running"}

    state = {}
    state_path = tmp_path / "ledger.json"
    result = ad["_dispatch"](provider=PROVIDER, receipt=RECEIPT, state=state,
                             bridge_post=producer, now=NOW, state_path=state_path,
                             fingerprints={}, engine=ENGINE, commissioning=commissioning)
    assert len(calls) == 1
    brief, model, timeout_s, _ = calls[0]
    assert brief.startswith("BRIEF for %s c1" % PROVIDER)
    assert ("REVIEWED COMMISSIONING BRIEF" in brief) is (commissioning is not None)
    assert (model, timeout_s) == ("fixture-model", 2700)
    assert result["dispatched"] is True
    assert result["reason_code"] == "dispatched"
    assert result["status"] == "in_flight"
    assert result["cycle"] == 1 and result["job_id"] == "job-1"
    assert result["commissioning"] == commissioning
    cycles = state["providers"][PROVIDER]["cycles"]
    assert len(cycles) == 1 and cycles[0]["job_id"] == "job-1"
    assert json.loads(state_path.read_text()) == state
