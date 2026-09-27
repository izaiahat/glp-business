"""Host/bridge transport fixtures only: never run the updater or publish."""
import json
import pytest
from pathlib import Path
from unittest.mock import Mock

from scripts import cron_bridge_action as cron

ACTION = "glp_seo_updater"
CONTRACT = json.loads((Path(__file__).parent / "fixtures/glp76-transport-contract.json").read_text())
ACTIONS = CONTRACT["registered_action"]


def job(status="BLOCKED_CURRENT_GENERATION_PLAN_MISSING", **fields):
    return {"job_id": "fixture-only", "status": "done", "rc": 0,
            "finished_utc": "2026-09-27T06:00:00Z", "receipt_path": "fixture-bridge-receipt",
            "stdout_tail": json.dumps({"schema": "n8n.seo_updater.v1", "status": status,
                                       "published": False, "refreshed": False, **fields})}


def run(tmp_path, call, args=None, **kwargs):
    return cron.run_action(ACTION, args or {}, state_dir=tmp_path, actions=ACTIONS,
                           call=call, notify=lambda *_: {"exact_readback": True}, **kwargs)


def test_semantic_release_refusal_is_not_rc_zero_success(tmp_path, monkeypatch):
    """Old September 3 authority is not upgraded by changing the transport."""
    calls = []
    def call(path, payload=None):
        calls.append((path, payload))
        return 200, job(reason="superseded release generation")
    hb, post = Mock(), Mock()
    monkeypatch.setattr(cron, "heartbeat", hb)
    monkeypatch.setattr(cron, "post_once", post)
    result = run(tmp_path, call, {"pt-date": "2026-09-03"})
    assert result["status"] == "FAIL" and result["rc"] != 0
    assert result["reason"] == "seo_updater_nonpass_see_machine_receipt"
    assert calls[0] == ("/run", {"action": "script", "name": ACTION,
                                   "args": {"pt-date": "2026-09-03"}})
    # A conclusively blocked machine can still have landed a partial effect.
    # Preserve its identity for GET-only reconciliation, not another POST.
    assert run(tmp_path, call, {"pt-date": "2026-09-03"})["status"] == "FAIL"
    assert [p for p, _ in calls] == ["/run", "/job/fixture-only"]
    hb.assert_not_called()
    post.assert_not_called()


def test_mode_admission_preserves_registered_arguments_without_a_schedule(tmp_path):
    call = Mock(return_value=(200, job()))
    invalid = [{"preview": "true"}, {"pt-date": ""}, {"pt-date": 20260903},
               {"review-request": "/fixture/request.json"},
               {"review-request-sha256": "a" * 64},
               {"review-request": "relative", "review-request-sha256": "a" * 64},
               {"review-request": "/fixture/request.json", "review-request-sha256": "bad"},
               {"pt-date": "2026-09-03", "review-request": "/fixture/request.json",
                "review-request-sha256": "a" * 64}]
    for args in invalid:
        result = run(tmp_path, call, args)
        assert result["status"] == "REFUSED_SEO_UPDATER_ARGS" and result["rc"] != 0
    result = run(tmp_path, call, invocation="scheduled", due_slot="fixture-slot")
    assert result["status"] == "REFUSED_SEO_UPDATER_UNSCHEDULED_ONLY"
    call.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_review_only_is_hash_bound_nonpublication_not_preview(tmp_path, monkeypatch):
    args = {"review-request": "/fixture/request.json", "review-request-sha256": "a" * 64}
    hb, post = Mock(), Mock()
    monkeypatch.setattr(cron, "heartbeat", hb)
    monkeypatch.setattr(cron, "post_once", post)
    for i, changes in enumerate([{}, {"published": True}, {"refreshed": True},
                                  {"review_only": False}, {"request_sha256": "b" * 64}]):
        fields = {"review_only": True, "request_sha256": "a" * 64,
                  "operation_id": "fixture-operation", "receipt_path": "/fixture/review.json"}
        fields.update(changes)
        call = Mock(return_value=(200, job("PASS_REVIEW_ONLY", **fields)))
        result = run(tmp_path / str(i), call, args)
        assert call.call_args.args[1] == {"action": "script", "name": ACTION, "args": args}
        if not changes:
            assert result["effects"] == {"machine_status": "PASS_REVIEW_ONLY", "review_only": True,
                                         "published": False, "output_count": 0,
                                         "receipt_path": "/fixture/review.json",
                                         "operation_id": "fixture-operation"}
        else:
            assert result["status"] == "FAIL"
    hb.assert_not_called()
    post.assert_not_called()


def test_noop_bookkeeping_counts_zero_not_publication(tmp_path, monkeypatch):
    hb, post = Mock(return_value={}), Mock(return_value={})
    monkeypatch.setattr(cron, "heartbeat", hb)
    monkeypatch.setattr(cron, "post_once", post)
    for i, status in enumerate(("PASS_NOTHING_DUE", "PASS_ALREADY_RUNNING", "PASS_DAILY_QUOTA_COMPLETE")):
        result = run(tmp_path / str(i), Mock(return_value=(200, job(status))))
        assert result["effects"]["output_count"] == 0
        assert result["effects"]["published"] is False
        assert hb.call_args.args[:4] == ("seo_update_carrier", 0, "fixture-bridge-receipt", status)
        assert "nothing published" in post.call_args.args[0]
    assert hb.call_count == post.call_count == 3
    for i, field in enumerate(("published", "refreshed")):
        result = run(tmp_path / ("bad" + str(i)), Mock(return_value=(200, job("PASS_NOTHING_DUE", **{field: True}))))
        assert result["status"] == "FAIL"
    assert hb.call_count == post.call_count == 3


def test_refresh_requires_exact_first_party_live_readback_before_bookkeeping(tmp_path, monkeypatch):
    import requests
    hb, post = Mock(return_value={}), Mock(return_value={})
    get = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(cron, "heartbeat", hb)
    monkeypatch.setattr(cron, "post_once", post)
    monkeypatch.setattr(requests, "get", get)
    route = "/guides/fixture-only/"
    fields = {"published": True, "refreshed": True, "route": route,
              "url": "https://glpclinicscout.com" + route,
              "receipt_path": "/fixture/final.json", "operation_id": "fixture-operation"}
    result = run(tmp_path / "pass", Mock(return_value=(200, job("PASS_REFRESHED", **fields))))
    assert result["effects"]["live_status"] == 200
    assert result["effects"]["output_count"] == 1
    get.assert_called_once_with(fields["url"], timeout=30, allow_redirects=False)
    assert hb.call_args.args[:4] == ("seo_update_carrier", 1, "/fixture/final.json", route)
    assert "1 article live" in post.call_args.args[0]
    for i, changes in enumerate([
        {"published": False}, {"url": "https://example.com" + route},
        {"url": "https://glpclinicscout.com/go/fixture/"},
        {"url": fields["url"] + "?tracking=fixture"},
        {"route": "/guides/another/"},
    ]):
        result = run(tmp_path / str(i), Mock(return_value=(200, job("PASS_REFRESHED", **{**fields, **changes}))))
        assert result["status"] == "FAIL"
    assert get.call_count == 1
    for status in (302, 404):
        get.return_value.status_code = status
        result = run(tmp_path / str(status), Mock(return_value=(200, job("PASS_REFRESHED", **fields))))
        assert result["status"] == "FAIL"
    assert hb.call_count == post.call_count == 1


def test_execution_19075_successor_cli_uses_bridge_without_n8n_limit(tmp_path, monkeypatch, capsys):
    """Regression for the actual refused webhook, NOT a live-effect claim."""
    import sys
    refused = CONTRACT["refused_execution"]
    assert (refused["id"], refused["nodeName"], refused["error_contains"]) == (
        "19075", "Manual Run", "Execution limit reached.")
    paths = []
    def call(path, payload=None):
        paths.append(path)
        if path == "/run":
            assert payload == {"action": "script", "name": ACTION, "args": {"pt-date": "2026-09-03"}}
            assert "callback_url" not in payload  # No n8n wait execution either.
            return 202, {"job_id": "fixture-only", "status": "running"}
        assert path == "/job/fixture-only"  # Reject the old webhook or any fallback.
        return 200, job("PASS_NOTHING_DUE")
    monkeypatch.setattr(cron, "heartbeat", Mock(return_value={}))
    monkeypatch.setattr(cron, "post_once", Mock(return_value={}))
    monkeypatch.setattr(cron, "make_call", lambda action, actions: call)
    monkeypatch.setattr(cron.time, "monotonic", Mock(side_effect=[100, 8000]))
    monkeypatch.setattr(cron.time, "sleep", Mock())
    real_run = cron.run_action
    def isolated(action, args, **kwargs):
        assert action == ACTION
        return real_run(action, args, state_dir=tmp_path, actions=ACTIONS, **kwargs)
    monkeypatch.setattr(cron, "run_action", isolated)
    monkeypatch.delenv("CRON_BRIDGE_MANUAL_REGISTERED", raising=False)
    monkeypatch.setattr(sys, "argv", ["cron_bridge_action.py", ACTION, "--arg", "pt-date=2026-09-03"])
    assert cron.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["invocation"] == "manual" and result["due_slot"] is None
    assert result["effects"]["published"] is False
    assert paths == ["/run", "/job/fixture-only"]
    # Poll after 7900 seconds proves the 8400-second registration is retained,
    # rather than the 300-second generic default. No real waiting occurred.


def test_existing_auth_loader_is_used_without_exposing_or_replacing_credentials(monkeypatch):
    import sys
    import types
    # Uncurated host dependencies are isolated at their transport boundary.
    # Do not supply a password/token file or silently use installed business code.
    token = object()
    helper = types.ModuleType("scripts.glp_onboarding_bridge_scoped_cutover")
    helper.bridge_call = Mock(return_value=(202, {"job_id": "fixture-only"}))
    bridge = types.ModuleType("fixture_existing_bridge")
    bridge.load_token = Mock(return_value=token)
    bridge.load_scoped_tokens = Mock(side_effect=AssertionError("wrong auth scope"))
    spec = Mock()
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    monkeypatch.setattr(cron.importlib.util, "spec_from_file_location", Mock(return_value=spec))
    monkeypatch.setattr(cron.importlib.util, "module_from_spec", Mock(return_value=bridge))
    call = cron.make_call(ACTION, ACTIONS)
    payload = {"action": "script", "name": ACTION, "args": {}}
    assert call("/run", payload)[0] == 202
    helper.bridge_call.assert_called_once_with("/run", token, payload)
    bridge.load_token.assert_called_once_with()
    bridge.load_scoped_tokens.assert_not_called()
    spec.loader.exec_module.assert_called_once_with(bridge)


def test_ambiguous_ack_and_bad_readbacks_never_repost(tmp_path, monkeypatch):
    hb, post = Mock(return_value={}), Mock(return_value={})
    monkeypatch.setattr(cron, "heartbeat", hb)
    monkeypatch.setattr(cron, "post_once", post)
    call = Mock(side_effect=TimeoutError("fixture lost acknowledgement"))
    for _ in range(2):
        assert run(tmp_path / "unknown", call)["status"] == "FAIL"
    assert call.call_count == 1
    cases = [{"stdout_truncated": True}, {"rc": None}, {"interrupted": True},
             {"status": "error", "rc": 0}, {"timed_out": True}]
    for i, changes in enumerate(cases):
        result = run(tmp_path / str(i), Mock(return_value=(200, {**job("PASS_NOTHING_DUE"), **changes})))
        assert result["status"] == "FAIL" and result["rc"] != 0
    hb.assert_not_called()
    post.assert_not_called()


@pytest.fixture(autouse=True)
def deny_live_effects(monkeypatch):
    import socket
    import subprocess
    def denied(*_args, **_kwargs):
        raise AssertionError("fixture attempted network or subprocess execution")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
