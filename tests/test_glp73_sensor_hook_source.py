"""Source-contract regressions only: synthetic receipts, no native sensor or network."""
import ast
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sensor = load("glp_deploy_completeness_sensor")
hook = load("glp_deploy_sensor_hook")


@pytest.fixture(autouse=True)
def deny_effects(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("unexpected external effect")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(hook, "post_ops_alert", denied)


def test_r1_requires_project_canonical_not_newest_successful(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    current = releases / "current"
    (releases / "predecessor").mkdir(parents=True)
    current.mkdir()
    target = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    pointer = {
        "schema": "glp-opus-current-content-base-v1", "status": "PASS",
        "deployment_id": target, "release_root": str(current),
        "release_tree_sha256": "a" * 64, "public_tree_sha256": "b" * 64,
        "manifest": {"path": str(tmp_path / "manifest.json"), "sha256": "c" * 64},
        "final_receipt": {"path": str(tmp_path / "final.json"), "sha256": "d" * 64},
        "supersedes": {"deployment_id": other, "release_root": str(releases / "predecessor")},
    }
    path = tmp_path / "current-content-base.json"
    path.write_text(json.dumps(pointer))
    monkeypatch.setattr(sensor, "CONTENT_BASE", path)
    monkeypatch.setattr(sensor, "RELEASES", releases)
    monkeypatch.setattr(sensor, "load_env_value", lambda _key: "fixture-token-not-sent")
    responses = []
    queries = []
    class Session:
        def __init__(self):
            self.headers = {}
        def get(self, url, **kwargs):
            queries.append((url, kwargs))
            return types.SimpleNamespace(json=lambda: responses.pop(0))
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(Session=Session))
    project = {"success": True, "result": {"canonical_deployment": {"id": target}}}
    responses[:] = [project]
    assert sensor.check_base_chain(offline=False)["status"] == "PASS"  # healthy witness
    responses[:] = [{"success": True, "result": {"canonical_deployment": {"id": other}}}]
    assert sensor.check_base_chain(offline=False)["status"] == "FAIL"  # known mismatch
    responses[:] = [
        {"success": True, "result": {"canonical_deployment": None}},
        {"success": True, "result": [{"id": target, "environment": "production",
            "is_skipped": False, "latest_stage": {"status": "success"}}]},
    ]
    result = sensor.check_base_chain(offline=False)
    assert result["status"] == "FAIL" and not result.get("live_canonical_deployment_id")
    assert len(queries) == 3 and all(not url.endswith("/deployments") for url, _ in queries)
    assert Path(sensor.validate_pointer_shape.__code__.co_filename) == SCRIPTS / "glp_current_content_base_schema.py"


def test_r2_raw_receipt_target_checks_and_freshness(tmp_path):
    target = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    release = str(tmp_path / "release")
    now = dt.datetime.now(dt.timezone.utc)

    def pair(*, captured=now, raw_status="PASS", skip=False, canonical=target):
        # The legacy hook had no required-check catalog; keep the retained RED
        # runnable against the custody commit without importing repaired code.
        names = getattr(hook, "LIVE_CHECKS", ("coverage", "surface_completeness",
            "publishable_provider_routes", "affiliate_surface_tracking",
            "provider_link_monetization", "therapeutic_facet_bindings", "count_copy",
            "derived_tallies", "research_binding", "freshness_stamps", "page_facts",
            "identity", "identity_dedup", "selectable_option_dedup", "link_graph",
            "score_binding", "aeo_surface", "base_chain"))
        checks = [{"check": name, "status": "PASS"} for name in names]
        checks[-1].update(base_deployment_id=target, live_canonical_deployment_id=canonical)
        if skip:
            checks[-1]["status"] = "SKIPPED"
        raw = {"schema": "glp-deploy-completeness-sensor-v1", "status": raw_status,
               "release": "release", "release_root": release,
               "captured_at_utc": captured.isoformat(), "checks": checks,
               "failure_count": 0, "failures": [], "failed_checks": [],
               "skipped_checks": ["base_chain"] if skip else []}
        body = json.dumps(raw, sort_keys=True).encode()
        file = tmp_path / "raw.json"
        file.write_bytes(body)
        summary = {key: raw[key] for key in ("schema", "status", "release", "release_root",
            "captured_at_utc", "failure_count", "failed_checks", "skipped_checks")}
        summary.update(receipt_path=str(file), receipt_sha256=hashlib.sha256(body).hexdigest(),
                       offline=False)
        return {"schema": "glp-enrichment-production-deployment-receipt-v1",
                "status": "PASS_DEPLOYED_READBACK_REQUIRED", "deployment_id": target,
                "release_root": release, "deployment": {"id": target,
                    "created_on": (now - dt.timedelta(seconds=2)).isoformat(),
                    "latest_stage": {"status": "success"}},
                "apply": {"exit_code": 0},
                "completed_at_utc": (now - dt.timedelta(seconds=1)).isoformat(),
                "sensor_result": summary}

    good = pair()
    assert hook.receipt_is_sensor_verified(good)[0] is True  # complete healthy control
    partial = pair(raw_status="PASS_PARTIAL", skip=True)
    assert hook.receipt_is_sensor_verified(partial)[0] is False
    partial["sensor_result"]["status"] = "PASS"  # summary laundering is not enough
    assert hook.receipt_is_sensor_verified(partial)[0] is False
    wrong = pair(canonical="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    assert hook.receipt_is_sensor_verified(wrong)[0] is False
    stale = pair(captured=now - dt.timedelta(hours=37))
    assert hook.receipt_is_sensor_verified(stale)[0] is False
    missing = pair()
    missing["sensor_result"].pop("receipt_sha256")
    assert hook.receipt_is_sensor_verified(missing)[0] is False
    wrong_release = pair()
    wrong_release["release_root"] = str(tmp_path / "other")
    assert hook.receipt_is_sensor_verified(wrong_release)[0] is False
    tampered = pair()
    Path(tampered["sensor_result"]["receipt_path"]).write_text("{}")
    assert hook.receipt_is_sensor_verified(tampered)[0] is False
    offline = pair(raw_status="PASS_PARTIAL", skip=True)
    offline["sensor_result"]["offline"] = True
    assert hook.receipt_is_sensor_verified(offline)[0] is False


def _complete_receipt(tmp_path, names, captured):
    target = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    release = str(tmp_path / "release")
    checks = [{"check": name, "status": "PASS"} for name in names]
    checks[-1].update(base_deployment_id=target, live_canonical_deployment_id=target)
    raw = {"schema": "glp-deploy-completeness-sensor-v1", "status": "PASS",
           "release": "release", "release_root": release,
           "captured_at_utc": captured.isoformat(), "checks": checks,
           "failure_count": 0, "failures": [], "failed_checks": [], "skipped_checks": []}
    path, digest = sensor.write_receipt(raw, tmp_path / "native.json")
    summary = {key: raw[key] for key in ("schema", "status", "release", "release_root",
                "captured_at_utc", "failure_count", "failed_checks", "skipped_checks")}
    summary.update(receipt_path=path, receipt_sha256=digest, offline=False)
    return {"deployment_id": target, "release_root": release, "sensor_result": summary}


def test_r2_capture_follows_bound_event_for_each_caller_schema(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    target = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    receipt = _complete_receipt(tmp_path, hook.LIVE_CHECKS, now - dt.timedelta(hours=35))
    receipt.update(schema="glp-enrichment-production-deployment-receipt-v1",
                   status="PASS_DEPLOYED_READBACK_REQUIRED", apply={"exit_code": 0},
                   deployment={"id": target, "created_on": (now - dt.timedelta(hours=1)).isoformat(),
                               "latest_stage": {"status": "success"}},
                   completed_at_utc=(now - dt.timedelta(minutes=50)).isoformat())
    assert hook.receipt_is_sensor_verified(receipt)[0] is False  # within TTL, before deploy
    old_event = {**receipt, "deployment": {**receipt["deployment"],
                 "created_on": (now - dt.timedelta(hours=35, minutes=10)).isoformat()},
                 "completed_at_utc": (now - dt.timedelta(hours=35, minutes=5)).isoformat()}
    assert hook.receipt_is_sensor_verified(old_event)[0] is True  # TTL is not shortened
    # Restore the raw hash/capture together, rather than laundering the summary.
    receipt["sensor_result"] = _complete_receipt(tmp_path, hook.LIVE_CHECKS, now)["sensor_result"]
    assert hook.receipt_is_sensor_verified(receipt)[0] is True
    for field, value in (("completed_at_utc", None), ("completed_at_utc", "bad"),
                         ("completed_at_utc", (now + dt.timedelta(minutes=1)).isoformat())):
        bad = {**receipt, field: value}
        assert hook.receipt_is_sensor_verified(bad)[0] is False
    bad = {**receipt, "deployment": {**receipt["deployment"], "latest_stage": {"status": "failed"}}}
    assert hook.receipt_is_sensor_verified(bad)[0] is False

    # Weekly completion follows the sensor, so it must not be its lower bound.
    weekly = {**receipt, "schema": "glp-price-index-release-chain-v1", "status": "REMINTED_SENSOR_PENDING",
              "completed_at_utc": (now + dt.timedelta(minutes=1)).isoformat(),
              "deployment": {"id": target, "deployment_id": target, "status": "success"},
              "external_effects": {"cloudflare_deployments": 1, "current_content_base_remints": 1},
              "remint": {"status": "PASS", "readback": {
                  "schema": "glp-opus-current-content-base-v1", "status": "PASS",
                  "deployment_id": target, "release_root": receipt["release_root"],
                  "reminted_at_utc": (now - dt.timedelta(seconds=30)).replace(microsecond=0).isoformat()}},
              "commands": [
                  {"command": f"npx wrangler@4.102.0 pages deploy {receipt['release_root']}/public --project-name glp-clinic-scout --branch main",
                   "started_at_utc": (now - dt.timedelta(seconds=50)).isoformat(), "exit_code": 0},
                  {"command": f"sudo -n python3 /home/ubuntu/business/systems/seo-blog-machine/scripts/remint_content_base.py --release {receipt['release_root']} --deployment-id {target}",
                   "started_at_utc": (now - dt.timedelta(seconds=40)).isoformat(), "exit_code": 0}]}
    assert hook.receipt_is_sensor_verified(weekly)[0] is True
    for changed in (
        {"remint": {**weekly["remint"], "readback": {**weekly["remint"]["readback"], "deployment_id": "other"}}},
        {"remint": {**weekly["remint"], "readback": {**weekly["remint"]["readback"], "reminted_at_utc": "bad"}}},
        {"commands": [weekly["commands"][0], {**weekly["commands"][1], "exit_code": 1}]},
        {"commands": [weekly["commands"][0], {**weekly["commands"][1], "started_at_utc": (now + dt.timedelta(seconds=1)).isoformat()}]},
    ):
        assert hook.receipt_is_sensor_verified({**weekly, **changed})[0] is False
    early = {**weekly, "sensor_result": _complete_receipt(
        tmp_path, hook.LIVE_CHECKS, now - dt.timedelta(minutes=1))["sensor_result"]}
    assert hook.receipt_is_sensor_verified(early)[0] is False


def _native_weekly_target_receipt(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    receipt = _complete_receipt(tmp_path, hook.LIVE_CHECKS, now)
    target, release = receipt["deployment_id"], receipt["release_root"]
    receipt.update(schema="glp-price-index-release-chain-v1", status="PASS",
                   release={"path": release}, deployment={"id": target, "deployment_id": target, "status": "success"},
                   external_effects={"cloudflare_deployments": 1, "current_content_base_remints": 1},
                   remint={"status": "PASS", "readback": {"schema": "glp-opus-current-content-base-v1",
                       "status": "PASS", "deployment_id": target, "release_root": release,
                       "reminted_at_utc": (now - dt.timedelta(minutes=9)).isoformat()}},
                   commands=[
                       {"command": f"npx wrangler@4.102.0 pages deploy {release}/public --project-name glp-clinic-scout --branch main",
                        "started_at_utc": (now - dt.timedelta(minutes=12)).isoformat(), "exit_code": 0},
                       {"command": f"sudo -n python3 /home/ubuntu/business/systems/seo-blog-machine/scripts/remint_content_base.py --release {release} --deployment-id {target} --supersedes-deployment-id bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb --supersedes-release {tmp_path}/predecessor",
                        "started_at_utc": (now - dt.timedelta(minutes=10)).isoformat(), "exit_code": 0}])
    return receipt


def test_r2_event_consistency_rejects_native_alias_and_deploy_time(tmp_path):
    good = _native_weekly_target_receipt(tmp_path)
    assert hook.receipt_is_sensor_verified(good)[0] is True
    alias = copy.deepcopy(good)
    alias["deployment"]["deployment_id"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert hook.receipt_is_sensor_verified(alias)[0] is False
    late_deploy = copy.deepcopy(good)
    captured = dt.datetime.fromisoformat(good["sensor_result"]["captured_at_utc"])
    late_deploy["commands"][0]["started_at_utc"] = (captured + dt.timedelta(minutes=3)).isoformat()
    assert hook.receipt_is_sensor_verified(late_deploy)[0] is False


def test_r2_remint_command_target_requires_unique_exact_arguments(tmp_path):
    good = _native_weekly_target_receipt(tmp_path)
    assert hook.receipt_is_sensor_verified(good)[0] is True
    release = good["release_root"]
    prefix = copy.deepcopy(good)
    prefix["commands"][1]["command"] = prefix["commands"][1]["command"].replace(
        f"--release {release} ", f"--release {release}-other ")
    assert hook.receipt_is_sensor_verified(prefix)[0] is False
    duplicate = copy.deepcopy(good)
    duplicate["commands"][1]["command"] += f" --release {release}-other"
    assert hook.receipt_is_sensor_verified(duplicate)[0] is False


def test_r2_native_producer_names_are_ordered_mandatory_pass(tmp_path):
    tree = ast.parse((SCRIPTS / "glp_deploy_completeness_sensor.py").read_text())
    producer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_sensor")
    runner = next(n for n in ast.walk(producer) if isinstance(n, ast.FunctionDef) and n.name == "run_checks")
    returns = [n.value for n in runner.body if isinstance(n, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0], ast.List)
    calls = returns[0].elts
    labels = []
    for call in calls:
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        bodies = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == call.func.id]
        assert len(bodies) == 1
        emitted = {c.args[0].value for c in ast.walk(bodies[0])
                   if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "check"
                   and c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)}
        assert len(emitted) == 1, (call.func.id, emitted)
        labels.append(emitted.pop())
    assert len(labels) == 18 and tuple(labels) == hook.LIVE_CHECKS
    now = dt.datetime.now(dt.timezone.utc)
    native = _complete_receipt(tmp_path, labels, now)
    native.update(schema="glp-enrichment-production-deployment-receipt-v1",
                  status="PASS_DEPLOYED_READBACK_REQUIRED", apply={"exit_code": 0},
                  deployment={"id": native["deployment_id"],
                              "created_on": (now - dt.timedelta(minutes=2)).isoformat(),
                              "latest_stage": {"status": "success"}},
                  completed_at_utc=(now - dt.timedelta(minutes=1)).isoformat())
    assert hook.receipt_is_sensor_verified(native)[0] is True
    missing = _complete_receipt(tmp_path, labels[:-1], now)
    missing.update({key: native[key] for key in ("schema", "status", "apply", "deployment", "completed_at_utc")})
    assert hook.receipt_is_sensor_verified(missing)[0] is False
