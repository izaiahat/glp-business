"""Offline writer-receipt fixtures; no Google, portal, or release execution."""
import importlib.util
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/affiliate_terms_fanout.py"
spec = importlib.util.spec_from_file_location("affiliate_terms_fanout_under_test", SOURCE)
fanout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fanout)


@pytest.fixture
def fixture(tmp_path):
    row = [""] * 27
    row[2], row[4], row[9], row[10] = "Example Clinic", "Awin", "example", "approved_active"
    row[16], row[17], row[24] = "$30 per approved sale", "", "2026-10-01"
    values = [["header"] * 27, row]
    queue = {"kind": "commission_terms_changed", "network": "Awin", "program_id": "00123", "update_id": "terms-1"}
    authority = fanout.digest(json.dumps(queue, sort_keys=True, separators=(",", ":")).encode())
    plan = {"queue_update_id": "terms-1", "authority_sha256": authority, "rows": [{"row": 2,
        "queue_update_id": "terms-1", "cells": {"16": row[16], "17": row[17]},
        "evidence": {"network_program_id": "00123"}}]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(fanout.encoded(plan))
    receipt = {"status": "PASS", "mode": "commission_terms_correction", "dry_run": False, "authority_sha256": authority,
        "readback_mismatches": [], "collateral_changes": [], "idempotent_rerun": True,
        "rows": [2], "queue_update_ids": ["terms-1"],
        "plan_sha256": fanout.digest(plan_path.read_bytes()),
        "post_write_values_sha256": fanout.values_sha(values),
        "captured_at_utc": "2026-10-01T00:00:00Z"}
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(fanout.encoded(receipt))
    event_path = tmp_path / "event.json"
    result = {"update_id": "terms-1", "sheet_row": 2}
    fanout.capture_readback(queue, result, plan_path, receipt_path, values, event_path)
    registry = {"programs": [{"program_id": "example", "display_name": "Example Clinic",
        "commercial_state": "live", "profiles": {"glp-clinic-scout": {"wired": True,
        "surfaces": {"go": True, "review": True, "sitemap": True}}}}]}
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(fanout.encoded(registry))
    mapping = tmp_path / "identity.json"
    mapping.write_bytes(fanout.encoded({"mappings": [{"network": "Awin", "program_id": "00123",
                                                    "registry_program_id": "example",
                                                    "selected_relationship": True}]}))
    return SimpleNamespace(event=event_path, registry=registry_path, mapping=mapping,
                           state=tmp_path / "state", receipt=receipt_path, plan=plan_path,
                           queue=queue, result=result, values=values)


def save_event(f, event):
    if event['kind'] != event['queue_source']['kind']:
        event['queue_source']['kind'] = event['kind']
        authority = fanout.digest(json.dumps(event['queue_source'], sort_keys=True, separators=(',', ':')).encode())
        plan = fanout.read(f.plan)
        plan['authority_sha256'] = authority
        f.plan.write_bytes(fanout.encoded(plan))
        receipt = fanout.read(f.receipt)
        receipt['authority_sha256'] = authority
        receipt['plan_sha256'] = fanout.digest(f.plan.read_bytes())
        f.receipt.write_bytes(fanout.encoded(receipt))
        event.update(plan=fanout.binding(f.plan), writer_receipt=fanout.binding(f.receipt))
    f.event.write_bytes(fanout.encoded(event))


def consume(f, **kwargs):
    return fanout.consume(f.event, f.registry, f.mapping, f.state, **kwargs)


def test_disabled_and_dry_run_have_no_writes_or_dispatch(fixture):
    f = fixture
    before = f.registry.read_bytes()
    forbidden = lambda *a: pytest.fail("release dispatcher invoked")
    assert consume(f, apply=True, dispatcher=forbidden)["status"] == "DISABLED"
    assert consume(f, enabled=True, dispatch_site=True, dispatcher=forbidden)["status"] == "DRY_RUN"
    assert f.registry.read_bytes() == before
    assert not f.state.exists()


def test_receipted_terms_update_registry_idempotently_preserves_unknown_and_permissions(fixture):
    f = fixture
    original = fanout.read(f.registry)["programs"][0]
    first = consume(f, enabled=True, apply=True)
    after = f.registry.read_bytes()
    program = fanout.read(f.registry)["programs"][0]
    assert program["affiliate_terms"]["awin:00123"]["commission_terms"] == "$30 per approved sale"
    assert program["affiliate_terms"]["awin:00123"]["cookie_window"] is None
    assert {k: v for k, v in program.items() if k != "affiliate_terms"} == original
    assert consume(f, enabled=True, apply=True)["registry_after_sha256"] == first["registry_after_sha256"]
    assert f.registry.read_bytes() == after
    assert len(list(f.state.glob("*.json"))) == 1


@pytest.mark.parametrize("field,value", [("status", "FAIL"), ("dry_run", True),
    ("idempotent_rerun", False), ("readback_mismatches", [{}]), ("collateral_changes", [{}])])
def test_untrustworthy_writer_receipt_never_propagates(fixture, field, value):
    f = fixture
    receipt = fanout.read(f.receipt)
    receipt[field] = value
    f.receipt.write_bytes(fanout.encoded(receipt))
    with pytest.raises(ValueError, match="not_trustworthy"):
        fanout.capture_readback(f.queue, f.result, f.plan, f.receipt, f.values, f.event)
    with pytest.raises(ValueError, match="hash_drift"):
        consume(f, enabled=True, apply=True)
    assert not f.state.exists()


def test_readback_drift_and_network_id_mismatch_are_refused(fixture):
    f = fixture
    event = fanout.read(f.event)
    event["sheet_values"][1][16] = "$999"
    save_event(f, event)
    with pytest.raises(ValueError, match="writer_binding_invalid"):
        consume(f, enabled=True, apply=True)
    event["sheet_values"] = f.values
    event["program_id"] = "123"  # opaque leading zeros cannot be normalized away
    save_event(f, event)
    with pytest.raises(ValueError, match="network_program_binding"):
        consume(f, enabled=True, apply=True)


@pytest.mark.parametrize("name", ["Fella Health", "Telos", "XenTraffic", "HealthRX"])
def test_provider_holds_never_mutate_or_trigger(fixture, name):
    f = fixture
    registry = fanout.read(f.registry)
    registry["programs"][0]["display_name"] = name
    f.registry.write_bytes(fanout.encoded(registry))
    before = f.registry.read_bytes()
    with pytest.raises(ValueError, match="exclusion_or_provider_hold"):
        consume(f, enabled=True, apply=True, dispatch_site=True,
                dispatcher=lambda *a: pytest.fail("held provider dispatch"))
    assert f.registry.read_bytes() == before


def test_exact_identity_required_no_display_name_join(fixture):
    f = fixture
    f.mapping.write_text('{"mappings": []}')
    with pytest.raises(ValueError, match="exact_identity_mapping_required"):
        consume(f, enabled=True, apply=True)


def test_dispatch_once_and_uncertain_effect_never_retried(fixture):
    f = fixture
    calls = []
    def dispatcher(request, handoff):
        calls.append(request["registry_after_sha256"])
        return {"returncode": 0}
    args = dict(enabled=True, apply=True, dispatch_site=True,
                release_handoff=Path("synthetic-review.json"), dispatcher=dispatcher)
    assert consume(f, **args)["status"] == "DISPATCHED"
    assert consume(f, **args)["status"] == "ALREADY_APPLIED"
    assert len(calls) == 1


def test_uncertain_dispatch_requires_receipt_reconciliation(fixture):
    f = fixture
    calls = []
    def dispatcher(*args):
        calls.append(1)
        raise TimeoutError("fixture interrupted after intent")
    args = dict(enabled=True, apply=True, dispatch_site=True,
                release_handoff=Path("synthetic-review.json"), dispatcher=dispatcher)
    with pytest.raises(TimeoutError):
        consume(f, **args)
    with pytest.raises(ValueError, match="dispatch_uncertain"):
        consume(f, **args)
    assert len(calls) == 1


def test_fixed_registered_carrier_and_review_hash_binding(fixture, tmp_path):
    f = fixture
    request = consume(f, enabled=True, apply=True)
    prepared = tmp_path / "prepared"
    (prepared / "release/data").mkdir(parents=True)
    (prepared / "release/data/program-registry.json").write_bytes(f.registry.read_bytes())
    (prepared / "PREPARED.json").write_text('{}')
    approval = tmp_path / "approval.json"
    approval.write_text('{"fixture_only":true}')
    handoff = tmp_path / "handoff.json"
    handoff.write_bytes(fanout.encoded({"registry_sha256": request["registry_after_sha256"],
        "prepared_dir": str(prepared), "expected_prepared_manifest_sha256": fanout.digest(b'{}'),
        "approval": str(approval), "expected_approval_sha256": fanout.digest(approval.read_bytes())}))
    calls = []
    terminal_receipt = tmp_path / "terminal.json"
    terminal_receipt.write_text('{"status":"PASS_PRODUCTION_TERMINAL"}')
    def runner(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is False
        output = {"status": "PASS_PRODUCTION_TERMINAL", "receipt": str(terminal_receipt),
                  "receipt_sha256": fanout.digest(terminal_receipt.read_bytes()),
                  "deployment_id": "fixture", "site_head": "fixture"}
        return SimpleNamespace(returncode=0, stdout=fanout.encoded(output), stderr=b"")
    assert fanout.dispatch(request, handoff, runner)["returncode"] == 0
    assert Path(calls[0][1]).name == "glp_deploy_glp34_authority_v1.py"
    assert "--publish-current" in calls[0] and "--authorize-production" in calls[0]
    approval.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="approval_drift"):
        fanout.dispatch(request, handoff, runner)
    assert len(calls) == 1


def test_registry_crash_recovery_and_cas_conflict(fixture):
    f = fixture
    consume(f, enabled=True, apply=True)
    registry = fanout.read(f.registry)
    registry["unrelated_editor_change"] = True
    f.registry.write_bytes(fanout.encoded(registry))
    with pytest.raises(ValueError, match="registry_cas_drift"):
        consume(f, enabled=True, apply=True)


def test_status_observation_never_grants_activation(fixture):
    f = fixture
    event = fanout.read(f.event)
    event["kind"] = "approval_confirmed"
    receipt = fanout.read(f.receipt)
    receipt["mode"] = "approval_confirmed"
    f.receipt.write_bytes(fanout.encoded(receipt))
    event["writer_receipt"] = fanout.binding(f.receipt)
    save_event(f, event)
    registry = fanout.read(f.registry)
    registry["programs"][0]["commercial_state"] = "research"
    f.registry.write_bytes(fanout.encoded(registry))
    consume(f, enabled=True, apply=True)
    assert fanout.read(f.registry)["programs"][0]["commercial_state"] == "research"


def test_empty_terms_remains_unknown(fixture):
    f = fixture
    event = fanout.read(f.event)
    event["kind"] = "approval_confirmed"
    event["sheet_values"][1][16] = ""
    plan = fanout.read(f.plan)
    plan["rows"][0]["cells"]["16"] = ""
    f.plan.write_bytes(fanout.encoded(plan))
    receipt = fanout.read(f.receipt)
    receipt.update(mode="approval_confirmed", plan_sha256=fanout.digest(f.plan.read_bytes()),
                   post_write_values_sha256=fanout.values_sha(event["sheet_values"]))
    f.receipt.write_bytes(fanout.encoded(receipt))
    event.update(plan=fanout.binding(f.plan), writer_receipt=fanout.binding(f.receipt))
    save_event(f, event)
    consume(f, enabled=True, apply=True)
    assert fanout.read(f.registry)["programs"][0]["affiliate_terms"]["awin:00123"]["commission_terms"] is None


def set_status_event(f, kind, status):
    event = fanout.read(f.event)
    event["kind"] = kind
    event["sheet_values"][1][10] = status
    plan = fanout.read(f.plan)
    plan["rows"][0]["cells"]["10"] = status
    f.plan.write_bytes(fanout.encoded(plan))
    receipt = fanout.read(f.receipt)
    receipt.update(mode=fanout.KINDS[kind], plan_sha256=fanout.digest(f.plan.read_bytes()),
                   post_write_values_sha256=fanout.values_sha(event["sheet_values"]))
    f.receipt.write_bytes(fanout.encoded(receipt))
    event.update(plan=fanout.binding(f.plan), writer_receipt=fanout.binding(f.receipt))
    save_event(f, event)


@pytest.mark.parametrize("kind,status", [("network_cancelled", "cancelled_by_network"),
                                        ("partner_email_hold", "hold_partner_email")])
def test_adverse_status_disables_commercial_surfaces_without_losing_review(fixture, kind, status):
    f = fixture
    set_status_event(f, kind, status)
    consume(f, enabled=True, apply=True)
    program = fanout.read(f.registry)["programs"][0]
    assert program["commercial_state"] == program["go"]["entry_state"] == "held"
    profile = program["profiles"]["glp-clinic-scout"]
    assert profile["wired"] is False
    assert all(profile["surfaces"][key] is False for key in ("go", "compare", "quiz", "calculator"))
    assert profile["surfaces"]["review"] is profile["surfaces"]["sitemap"] is True
    assert all(profile["exceptions"][key]["authority_sha256"] for key in profile["exceptions"])


def test_adverse_status_needs_selected_relationship_crosswalk(fixture):
    f = fixture
    mapping = fanout.read(f.mapping)
    mapping["mappings"][0].pop("selected_relationship")
    f.mapping.write_bytes(fanout.encoded(mapping))
    set_status_event(f, "network_cancelled", "cancelled_by_network")
    with pytest.raises(ValueError, match="selected_relationship_required"):
        consume(f, enabled=True, apply=True)


def test_positive_status_never_reactivates_existing_registry_hold(fixture):
    f = fixture
    registry = fanout.read(f.registry)
    program = registry["programs"][0]
    program["commercial_state"] = "held"
    program["profiles"]["glp-clinic-scout"]["wired"] = False
    program["profiles"]["glp-clinic-scout"]["surfaces"]["go"] = False
    f.registry.write_bytes(fanout.encoded(registry))
    set_status_event(f, "network_reactivated", "approved_active")
    consume(f, enabled=True, apply=True)
    after = fanout.read(f.registry)["programs"][0]
    assert after["commercial_state"] == "held"
    assert after["profiles"] == program["profiles"]


@pytest.mark.parametrize("adverse", [False, True])
def test_native_site_registry_shape_accepts_commercial_projection(fixture, adverse):
    f = fixture
    registry = fanout.read(f.registry)
    registry.update(schema="site-program-registry-v1", site_profiles={
        "glp-clinic-scout": {"auto_generate": False, "auto_publish": False}})
    program = registry["programs"][0]
    program.update(review_path="/reviews/example/", go={"route": "/go/example", "entry_state": "transparent_direct"})
    profile = program["profiles"]["glp-clinic-scout"]
    profile.update(vertical_fit=True, data_profile={"has_price": False, "has_review_facts": True})
    profile["surfaces"] = {key: True for key in ("go", "compare", "quiz", "calculator", "review", "sitemap")}
    f.registry.write_bytes(fanout.encoded(registry))
    if adverse:
        set_status_event(f, "network_cancelled", "cancelled_by_network")
    consume(f, enabled=True, apply=True)
    validator = Path(__file__).parent / "fixtures/affiliate_terms_registry_validator.mjs"
    code = "import {validateRegistryShape} from " + json.dumps(validator.as_uri()) + ";\n"
    code += "let raw=''; for await (const part of process.stdin) raw+=part; validateRegistryShape(JSON.parse(raw),'glp-clinic-scout');"
    result = subprocess.run(["node", "--input-type=module", "-e", code], input=f.registry.read_text(),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("enabled", [False, True])
def test_actual_drain_function_captures_only_applied_writer_readback(fixture, monkeypatch, enabled):
    """Real maintained drain body; fixture writer/Google transport, no credentials."""
    f = fixture
    source = SOURCE.with_name("glp_sheet_sync_drain.py")
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "drain")
    discovery = SimpleNamespace(build=lambda *a, **kw: object())
    monkeypatch.setitem(sys.modules, "googleapiclient", SimpleNamespace(discovery=discovery))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setitem(sys.modules, "affiliate_terms_fanout", fanout)
    monkeypatch.setenv("GLP_AFFILIATE_TERMS_FANOUT_ENABLE", "1" if enabled else "0")
    for name, path in (("REGISTRY", f.registry), ("IDENTITY_MAP", f.mapping), ("STATE_DIR", f.state)):
        monkeypatch.setenv("GLP_AFFILIATE_TERMS_" + name, str(path))
    plan = fanout.read(f.plan)
    def fixture_writer(mode, plan, plan_path, receipt_path, apply, **kwargs):
        plan_path.write_bytes(fanout.encoded(plan))
        receipt = fanout.read(f.receipt)
        receipt.update(plan_sha256=fanout.digest(plan_path.read_bytes()), value_cells_written=1)
        receipt_path.write_bytes(fanout.encoded(receipt))
        return {"rc": 0, "receipt": receipt}
    namespace = dict(time=time, os=os, Path=Path, Any=object, DRAIN_BUDGET_S=60,
        RESULT_APPLIED="applied", RESULT_NOOP="noop", RESULT_REFUSED="refused", RESULT_ESCALATED="escalated",
        RESULT_DEFERRED="queued", SCHEMA="glp.sheet_sync_drain.v1", utc_now=lambda: "fixture-time",
        require_katalys_queue_id_consistency=lambda rows: None,
        writer=SimpleNamespace(creds=lambda: None, SHEET_SYNC_KIND_MODE=fanout.KINDS, pad=lambda x: x),
        read_sheet=lambda *a, **kw: f.values,
        result_base=lambda row, index: {"update_id": "terms-1", "kind": row["kind"], "sheet_row": None,
                                       "cells_written": 0, "status": "escalated"},
        parse_evidence=lambda row: {}, materialize_evidence=lambda *a: {},
        resolve_sheet_row=lambda *a: (2, None), lifecycle_supersession_reason=lambda *a: None,
        build_entry=lambda *a: {}, plan_for=lambda *a: plan,
        run_mode_with_retry=fixture_writer, repo_relative=str)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    run_dir = f.event.parent / "drain"
    run_dir.mkdir()
    result = namespace["drain"]([f.queue], run_dir, True)["results"][0]
    assert result["status"] == "applied"
    _, event = fanout.bound(result["terms_fanout_event"])
    assert event["sheet_values"] == f.values
    assert result["terms_fanout"]["status"] == ("PENDING_REVIEWED_SITE_RELEASE" if enabled else "DISABLED")
    if enabled:
        assert fanout.read(f.registry)["programs"][0]["affiliate_terms"]["awin:00123"]
