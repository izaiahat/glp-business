"""Offline regression: historical pins must not replace current producer custody."""
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def load_worker_functions():
    # The runtime worker imports host-only adapters. Exercise the real maintained
    # function bodies without importing those unrelated effect-capable adapters.
    tree = ast.parse((ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text())
    names = {"best_terms_morning_stage", "verify_binding"}
    selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    scope = {"Path": Path, "Any": object, "B2Error": ValueError,
             "BEST_TERMS_MORNING_STAGE": Path("absent-historical-stage"),
             "read_json": lambda p: json.loads(p.read_text()),
             "sha_file": lambda p: hashlib.sha256(p.read_bytes()).hexdigest(),
             "normalize_network": lambda s: str(s).lower()}
    exec(compile(selected, "maintained-worker-functions", "exec"), scope)
    return scope


@pytest.fixture
def packet(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    generated = now.isoformat()
    window = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    matrix = write(tmp_path / "runs/current/matrix.json", {"schema": "glp.best-collectible-offer-selection.v1", "providers": [], "generated_at_utc": generated})
    backlog = write(tmp_path / "switch-backlog.json", {"schema": "glp.best-terms-switch-backlog.v1", "items": []})
    receipt = {"schema": "glp.best-terms-run.v1", "status": "PARTIAL", "mode": "weekly",
               "generated_at_utc": generated, "weekly_window": {"window_id": window, "duplicate": False},
               "matrix": matrix, "switch_backlog": backlog}
    run = write(tmp_path / "runs/current/run-receipt.json", receipt)
    latest = tmp_path / "latest-run.json"
    write(latest, {**receipt, "run_receipt": run})
    return SimpleNamespace(root=tmp_path, matrix=matrix, backlog=backlog, receipt=receipt, run=run, latest=latest)


def test_missing_historical_packet_rebound_to_authenticated_current_producer(tmp_path, packet):
    scope = load_worker_functions()
    active = write(tmp_path / "active.json", {"programs": []})
    audit = write(tmp_path / "audit.json", {"captured": True})
    stage = tmp_path / "stage.json"
    write(stage, {"schema": "glp.wire-in-best-terms-reconciliation-stage.v1", "status": "STAGED_NOT_DEPLOYED",
                 "source_bindings": {"active_rail_selection": active, "payout_audit": audit,
                                     "deploy_packet_manifest": {"path": str(tmp_path / "deleted/manifest.json"), "sha256": "0" * 64},
                                     "matrix_data": {"path": str(tmp_path / "deleted/matrix-data.json"), "sha256": "1" * 64}},
                 "reconciliations": [{"provider": "Example", "site_program_id": "example", "winner": {"sheet_row": 2, "network": "awin", "program_id": "10", "payout_usd": 20}, "losers": [{"sheet_row": 3, "network": "awin", "program_id": "11", "payout_usd": 10}]}]})
    def current_bindings():
        # Import lazily: the RED must be the historical binding drift itself.
        import importlib.util
        spec = importlib.util.spec_from_file_location("glp93_custody", ROOT / "scripts/glp_best_terms_current_bindings.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.current_best_terms_bindings(packet.latest, business=packet.root)
    scope["current_best_terms_bindings"] = current_bindings
    result = scope["best_terms_morning_stage"](stage)
    assert result["winners"][2]["program_id"] == "10"
    assert result["losers"][3]["program_id"] == "11"
    assert result["source_bindings"]["matrix_data"] == packet.matrix
    assert result["source_bindings"]["deploy_packet_manifest"] == packet.run
    assert result["source_bindings"]["active_rail_selection"] == active
    assert result["source_bindings"]["payout_audit"] == audit


def reader():
    import importlib.util
    spec = importlib.util.spec_from_file_location("glp93_custody", ROOT / "scripts/glp_best_terms_current_bindings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.current_best_terms_bindings


@pytest.mark.parametrize("label", ["active_rail_selection", "payout_audit"])
@pytest.mark.parametrize("action", ["missing", "tampered", "omitted"])
def test_existing_stage_bindings_remain_mandatory(tmp_path, label, action):
    scope = load_worker_functions()
    bindings = {key: write(tmp_path / (key + '.json'), {"programs": []})
                for key in ("active_rail_selection", "payout_audit")}
    if action == "missing":
        Path(bindings[label]["path"]).unlink()
    elif action == "tampered":
        Path(bindings[label]["path"]).write_text('{}')
    else:
        bindings.pop(label)
    stage = tmp_path / 'stage.json'
    write(stage, {"schema": "glp.wire-in-best-terms-reconciliation-stage.v1", "status": "STAGED_NOT_DEPLOYED", "source_bindings": bindings})
    scope["current_best_terms_bindings"] = lambda: {}
    with pytest.raises(ValueError, match=label):
        scope["best_terms_morning_stage"](stage)


@pytest.mark.parametrize("key", ["matrix", "backlog", "run"])
@pytest.mark.parametrize("action", ["missing", "tampered"])
def test_current_artifacts_never_rehashed_or_skipped(packet, key, action):
    path = Path(getattr(packet, key)["path"])
    if action == "missing":
        path.unlink()
    else:
        path.write_text(path.read_text() + " ")
    with pytest.raises((ValueError, OSError)):
        reader()(packet.latest, business=packet.root)


def reseal(packet):
    run = write(Path(packet.run["path"]), packet.receipt)
    write(packet.latest, {**packet.receipt, "run_receipt": run})


@pytest.mark.parametrize("mutation", ["failed", "provider_scoped", "matrix_schema", "switch_schema", "violations", "missing_hash", "foreign_matrix", "foreign_receipt", "pointer_disagreement"])
def test_untrusted_or_inconsistent_current_packet_fails_closed(packet, mutation):
    if mutation == "failed":
        packet.receipt["status"] = "FAIL"
    elif mutation == "provider_scoped":
        packet.receipt["mode"] = "approval_trigger"
    elif mutation in {"matrix_schema", "violations"}:
        matrix = {"schema": "invalid" if mutation == "matrix_schema" else "glp.best-collectible-offer-selection.v1", "providers": [], "winner_eligibility_violations": ["bad"] if mutation == "violations" else []}
        packet.receipt["matrix"] = write(Path(packet.matrix["path"]), matrix)
    elif mutation == "switch_schema":
        packet.receipt["switch_backlog"] = write(Path(packet.backlog["path"]), {"schema": "invalid", "items": []})
    elif mutation == "missing_hash":
        packet.receipt["matrix"].pop("sha256")
    elif mutation == "foreign_matrix":
        packet.receipt["matrix"] = write(packet.root / "foreign/matrix.json", {"schema": "glp.best-collectible-offer-selection.v1", "providers": []})
    reseal(packet)
    if mutation == "foreign_receipt":
        latest = json.loads(packet.latest.read_text())
        latest["run_receipt"] = write(packet.root / "foreign/run-receipt.json", packet.receipt)
        write(packet.latest, latest)
    elif mutation == "pointer_disagreement":
        latest = json.loads(packet.latest.read_text())
        latest["matrix"]["sha256"] = "a" * 64
        write(packet.latest, latest)
    with pytest.raises(ValueError):
        reader()(packet.latest, business=packet.root)


def test_producer_relative_paths_and_partial_do_not_mean_eligible(packet):
    for label in ("matrix", "switch_backlog"):
        packet.receipt[label]["path"] = str(Path(packet.receipt[label]["path"]).relative_to(packet.root))
    reseal(packet)
    latest = json.loads(packet.latest.read_text())
    latest["run_receipt"]["path"] = str(Path(latest["run_receipt"]["path"]).relative_to(packet.root))
    write(packet.latest, latest)
    result = reader()(packet.latest, business=packet.root)
    assert set(result) == {"best_terms_current_pointer", "deploy_packet_manifest", "matrix_data", "best_terms_current_switch_manifest"}
    assert "eligible" not in result


def set_packet_clock(packet, generated, window):
    matrix = json.loads(Path(packet.matrix["path"]).read_text())
    matrix["generated_at_utc"] = generated
    packet.receipt["matrix"] = write(Path(packet.matrix["path"]), matrix)
    packet.receipt["generated_at_utc"] = generated
    packet.receipt["weekly_window"] = {"window_id": window, "duplicate": False}
    reseal(packet)


@pytest.mark.parametrize("fault", ["old", "future", "missing_clock", "wrong_window"])
def test_review_freshness_counterexamples_refuse(packet, fault):
    # Same hash-consistent synthetic faults as RV565/test_freshness.py.
    if fault in {"old", "future"}:
        year = "2020" if fault == "old" else "2099"
        set_packet_clock(packet, year + "-01-01T00:00:00+00:00", year + "-W01")
    elif fault == "missing_clock":
        packet.receipt.pop("generated_at_utc")
        packet.receipt.pop("weekly_window")
        reseal(packet)
    else:
        packet.receipt["weekly_window"]["window_id"] = "2020-W01"
        reseal(packet)
    with pytest.raises(ValueError, match="best_terms_current_.*(clock|window|expired|future)"):
        reader()(packet.latest, business=packet.root)


def test_2020_packet_never_reaches_stage_success(tmp_path, packet):
    set_packet_clock(packet, "2020-01-01T00:00:00+00:00", "2020-W01")
    scope = load_worker_functions()
    scope["current_best_terms_bindings"] = lambda: reader()(packet.latest, business=packet.root)
    active = write(tmp_path / "active.json", {"programs": []})
    audit = write(tmp_path / "audit.json", {"fixture": True})
    stage = tmp_path / "stage.json"
    write(stage, {"schema": "glp.wire-in-best-terms-reconciliation-stage.v1", "status": "STAGED_NOT_DEPLOYED",
                  "source_bindings": {"active_rail_selection": active, "payout_audit": audit,
                                      "deploy_packet_manifest": {"path": str(tmp_path / "missing.json"), "sha256": "0" * 64}},
                  "reconciliations": [{"provider": "Example", "site_program_id": "example",
                                       "winner": {"sheet_row": 2, "network": "awin", "program_id": "10", "payout_usd": 20},
                                       "losers": [{"sheet_row": 3, "network": "awin", "program_id": "11", "payout_usd": 10}]}]})
    with pytest.raises(ValueError, match="best_terms_current_.*(expired|window)"):
        scope["best_terms_morning_stage"](stage)


@pytest.mark.parametrize("fault,reason", [
    ("receipt_missing", "clock_invalid"), ("matrix_missing", "clock_invalid"),
    ("receipt_null", "clock_invalid"), ("matrix_null", "clock_invalid"),
    ("receipt_naive", "clock_invalid"), ("matrix_naive", "clock_invalid"),
    ("receipt_date_only", "clock_invalid"), ("matrix_date_only", "clock_invalid"),
    ("receipt_malformed", "clock_invalid"), ("matrix_malformed", "clock_invalid"),
    ("receipt_future", "future_clock"), ("matrix_future", "future_clock"),
    ("receipt_expired", "expired_clock"), ("matrix_expired", "expired_clock"),
    ("matrix_newer", "clock_order_invalid"), ("matrix_previous_week", "window_invalid"),
    ("receipt_previous_week", "window_invalid"), ("window_missing", "window_invalid"),
    ("window_malformed", "window_invalid"), ("window_id_missing", "window_invalid"),
])
def test_individual_clock_and_window_guards(packet, monkeypatch, fault, reason):
    # Fixed Sunday: prior-week evidence is under seven days old, and a future
    # microsecond is still in this week. Neither may hide behind the other gate.
    now = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)
    read = reader()
    frozen = SimpleNamespace(datetime=SimpleNamespace(fromisoformat=dt.datetime.fromisoformat, now=lambda tz: now),
                             timezone=dt.timezone, timedelta=dt.timedelta)
    monkeypatch.setitem(read.__globals__, "dt", frozen)
    set_packet_clock(packet, (now - dt.timedelta(seconds=2)).isoformat(), "2026-W42")
    matrix = json.loads(Path(packet.matrix["path"]).read_text())
    if fault.startswith("window"):
        if fault == "window_missing":
            packet.receipt.pop("weekly_window")
        elif fault == "window_malformed":
            packet.receipt["weekly_window"] = "2026-W42"
        else:
            packet.receipt["weekly_window"] = {}
    else:
        target, mutation = fault.split("_", 1)
        document = packet.receipt if target == "receipt" else matrix
        values = {"null": None, "naive": "2026-10-18T11:59:58", "date_only": "2026-10-18",
                  "malformed": "not-a-date", "future": (now + dt.timedelta(microseconds=1)).isoformat(),
                  "expired": (now - dt.timedelta(days=7, microseconds=1)).isoformat(),
                  "newer": (now - dt.timedelta(seconds=1)).isoformat(),
                  "previous_week": "2026-10-11T23:59:59Z"}
        if mutation == "missing":
            document.pop("generated_at_utc")
        else:
            document["generated_at_utc"] = values[mutation]
    packet.receipt["matrix"] = write(Path(packet.matrix["path"]), matrix)
    reseal(packet)
    with pytest.raises(ValueError, match="best_terms_current_" + reason):
        read(packet.latest, business=packet.root)


@pytest.mark.parametrize("now,generated,window", [
    ("2026-10-18T23:59:59Z", "2026-10-12T00:00:00Z", "2026-W42"),
    ("2026-10-19T00:00:00Z", "2026-10-19T00:00:00Z", "2026-W43"),
    ("2027-01-01T12:00:00Z", "2027-01-01T07:00:00-05:00", "2026-W53"),
])
def test_current_window_boundaries_and_utc_normalization(packet, monkeypatch, now, generated, window):
    read = reader()
    current = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    frozen = SimpleNamespace(datetime=SimpleNamespace(fromisoformat=dt.datetime.fromisoformat, now=lambda tz: current),
                             timezone=dt.timezone, timedelta=dt.timedelta)
    monkeypatch.setitem(read.__globals__, "dt", frozen)
    set_packet_clock(packet, generated, window)
    assert len(read(packet.latest, business=packet.root)) == 4


def test_producer_matrix_can_precede_receipt_within_the_same_week(packet):
    generated = dt.datetime.fromisoformat(packet.receipt["generated_at_utc"])
    matrix = json.loads(Path(packet.matrix["path"]).read_text())
    matrix["generated_at_utc"] = (generated - dt.timedelta(seconds=1)).isoformat()
    packet.receipt["matrix"] = write(Path(packet.matrix["path"]), matrix)
    reseal(packet)
    assert len(reader()(packet.latest, business=packet.root)) == 4


def test_plan_persists_the_exact_verified_packet_bindings():
    tree = ast.parse((ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text())
    # Assert the real planner consumes returned verified bindings, rather than
    # rehashing whatever bytes happen to occupy the current paths afterward.
    assert any(isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
               and n.value.id == "morning_stage" and isinstance(n.slice, ast.Constant)
               and n.slice.value == "source_bindings" for n in ast.walk(tree))
