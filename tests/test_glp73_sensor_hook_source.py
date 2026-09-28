"""Source-contract regressions only: synthetic receipts, no native sensor or network."""
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
        return {"deployment_id": target, "release_root": release, "sensor_result": summary}

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
