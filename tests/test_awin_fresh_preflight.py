"""Provider-free contracts; fixture responses are not live sign-in evidence."""
import importlib
import json
from pathlib import Path

import pytest


def load():
    return importlib.import_module("scripts.awin_fresh_preflight")


def test_api_first_uses_maintained_adapter_and_minimizes_output(monkeypatch):
    m = load()
    from scripts.marketplace_adapters import awin, _base
    monkeypatch.setattr(awin, "_headers", lambda: {})
    transport = _base.RecordingTransport()
    transport.add("GET", m.API_URL, _base.json_response([
        {"id": 123, "name": "Fixture merchant", "displayUrl": "private-not-output"}
    ]))
    result = m.api_probe(transport)
    assert result["api_state"] == "PASS"
    assert result["portal_state"] == "NOT_CHECKED"
    assert result["publisher_id"] == "2979387"
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["params"] == {"relationship": "joined"}
    assert "private-not-output" not in json.dumps(result)
    assert "Fixture merchant" not in json.dumps(result)


@pytest.mark.parametrize("payload", [{"error": "fixture-private"}, None, [{}],
                                      [{"name": "no id"}], "login html"])
def test_malformed_200_is_not_api_success(monkeypatch, payload):
    m = load()
    from scripts.marketplace_adapters import awin, _base
    monkeypatch.setattr(awin, "_headers", lambda: {})
    transport = _base.RecordingTransport([
        ("GET", m.API_URL, _base.json_response(payload))])
    result = m.api_probe(transport)
    assert result["api_state"] == "UNAVAILABLE"
    assert result["portal_state"] == "NOT_CHECKED"
    assert "fixture-private" not in json.dumps(result)


def test_redirect_cannot_prove_api_readiness(monkeypatch):
    m = load()
    from scripts.marketplace_adapters import awin, _base
    monkeypatch.setattr(awin, "_headers", lambda: {})
    response = _base.json_response([{"id": 1, "name": "fixture"}])
    response.url = "https://evil.example/programmes"
    assert m.api_probe(_base.RecordingTransport([
        ("GET", m.API_URL, response)]))["api_state"] == "UNAVAILABLE"


def test_missing_credentials_and_transport_exceptions_are_sanitized(monkeypatch):
    m = load()
    from scripts.marketplace_adapters import awin
    def fail():
        raise RuntimeError("fixture-private-token")
    monkeypatch.setattr(awin, "_headers", fail)
    result = m.api_probe()
    assert result["api_state"] == "UNAVAILABLE"
    assert "fixture-private-token" not in json.dumps(result)


def test_reviewed_source_pins_fail_closed_before_provider_io(tmp_path):
    m = load()
    import hashlib
    pins = {}
    for name in m.SOURCE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture source")
        pins[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = tmp_path / "ops/awin-fresh-preflight.candidate.json"
    candidate.write_text(json.dumps({"source_sha256": pins}))
    m.verify_source(tmp_path)
    (tmp_path / m.SOURCE_FILES[0]).write_text("drift")
    with pytest.raises(ValueError, match="source_pin_mismatch"):
        m.verify_source(tmp_path)
    candidate.write_text(json.dumps({"source_sha256": {}}))
    with pytest.raises(ValueError, match="source_pin_set_mismatch"):
        m.verify_source(tmp_path)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_http_failure_is_never_success(monkeypatch, status):
    m = load()
    from scripts.marketplace_adapters import awin, _base
    monkeypatch.setattr(awin, "_headers", lambda: {})
    transport = _base.RecordingTransport([
        ("GET", m.API_URL, _base.json_response([], status=status))])
    assert m.api_probe(transport)["api_state"] == "UNAVAILABLE"


@pytest.mark.parametrize("method,url,params", [
    ("POST", "https://api.awin.com/accounts", {}),
    ("GET", "https://api.awin.com/publishers/123/programmes", {"relationship": "joined"}),
    ("GET", "https://www.awin1.com/cread.php", {"relationship": "joined"}),
])
def test_unexpected_requests_refused_before_transport(method, url, params):
    m = load()
    from scripts.marketplace_adapters._base import RecordingTransport
    delegate = RecordingTransport()
    with pytest.raises(ValueError, match="unexpected_api_request"):
        m.ReadOnlyTransport(delegate).request(method, url, params=params)
    assert delegate.calls == []


def test_production_transport_disables_redirects_and_enforces_one_get(monkeypatch):
    m = load()
    import requests
    from types import SimpleNamespace
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=b"[]", url=url)
    monkeypatch.setattr(requests, "get", get)
    guarded = m.ReadOnlyTransport()
    guarded.request("GET", m.API_URL, params={"relationship": "joined"})
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 30
    with pytest.raises(ValueError, match="api_request_budget_exceeded"):
        guarded.request("GET", m.API_URL, params={"relationship": "joined"})
    assert len(calls) == 1


def test_candidate_is_paused_review_only_with_exact_source_and_model_pins():
    import hashlib
    root = Path(__file__).resolve().parents[1]
    packet = json.loads((root / "ops/awin-fresh-preflight.candidate.json").read_text())
    updates = packet["proposed_updates"]
    assert packet["activation_authority"] is False
    assert packet["requested_job_id"] != packet["observed_paused_job_id"]
    assert updates["no_agent"] is False
    assert updates["script"] is None and updates["script_sha256"] is None
    assert updates["enabled"] is False and updates["state"] == "paused"
    assert updates["model"] == "gpt-6-astra-900k"
    assert updates["provider"] == "openai-codex"
    assert updates["reasoning_effort"] == "medium"
    assert set(packet["retired_job_ids_never_update"]) == {"52e2014a9579", "5a2cba16e057"}
    assert not {"schedule", "next_run_at", "deliver", "paused_reason"} & updates.keys()
    digest = hashlib.sha256(json.dumps(packet["source_sha256"], sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert digest in updates["prompt"]
    load().verify_source(root)
