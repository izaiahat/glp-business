from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import urllib.error
from copy import deepcopy
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/glp_affiliate_postback_n8n_sync.py"
spec = importlib.util.spec_from_file_location("glp_affiliate_postback_n8n_sync_test", SOURCE)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def _live(candidate: dict) -> dict:
    base = json.loads(json.dumps(candidate))
    return {
        **base,
        "id": "workflow-id",
        "active": True,
        "versionId": "version-1",
        "activeVersionId": "version-1",
        "activeVersion": {
            "id": "version-1",
            "nodes": json.loads(json.dumps(base["nodes"])),
            "connections": json.loads(json.dumps(base["connections"])),
        },
    }


def test_workflow_uses_header_get_query_post_query_get_and_identity_only_bridge_dispatch():
    candidate = sync.build_workflow("credential-id", "a" * 64)
    webhooks = [node for node in candidate["nodes"] if node["type"] == "n8n-nodes-base.webhook"]
    by_edge = {
        (node["parameters"]["path"], node["parameters"]["httpMethod"]): node
        for node in webhooks
    }
    assert set(by_edge) == {
        (sync.WEBHOOK_PATH, "GET"),
        (sync.WEBHOOK_PATH, "POST"),
        (sync.QUERY_WEBHOOK_PATH, "GET"),
    }
    header_get = by_edge[(sync.WEBHOOK_PATH, "GET")]
    assert header_get["parameters"]["authentication"] == "headerAuth"
    assert header_get["credentials"]["httpHeaderAuth"] == {
        "id": "credential-id",
        "name": sync.CREDENTIAL_NAME,
    }
    for edge in ((sync.WEBHOOK_PATH, "POST"), (sync.QUERY_WEBHOOK_PATH, "GET")):
        assert by_edge[edge]["parameters"]["authentication"] == "none"
        assert "credentials" not in by_edge[edge]
    for webhook_name in ("Query-Authenticated Postback POST", "Query-Authenticated Postback GET"):
        first_node = candidate["connections"][webhook_name]["main"][0][0]
        assert first_node["node"] == "Verify Query Secret and Strip"
    verifier = next(node for node in candidate["nodes"]
                    if node["name"] == "Verify Query Secret and Strip")
    assert "allowedQueryKeys" in verifier["parameters"]["jsCode"]
    assert "return [{json: {body, query: cleanQuery}}]" in verifier["parameters"]["jsCode"]
    builder = next(node for node in candidate["nodes"] if node["name"] == "Build Sanitized Bridge Request")
    assert "affiliate_postback_receive" in builder["parameters"]["jsCode"]
    assert "n8n_authenticated_edge_v2" in builder["parameters"]["jsCode"]
    assert "JSON.stringify(payload)" in builder["parameters"]["jsCode"]
    writer = next(node for node in candidate["nodes"] if node["name"] == "Run Durable Ledger Writer")
    body = writer["parameters"]["jsonBody"]
    assert "JSON.stringify($json)" in body
    assert "headers" not in body.lower()
    assert writer["credentials"]["httpHeaderAuth"] == sync.BRIDGE_CREDENTIAL


def test_workflow_disables_success_error_and_manual_execution_persistence():
    settings = sync.build_workflow("credential-id")["settings"]
    assert settings["saveDataSuccessExecution"] == "none"
    assert settings["saveDataErrorExecution"] == "none"
    assert settings["saveManualExecutions"] is False
    assert settings["saveExecutionProgress"] is False


def test_workflow_json_contains_no_secret_literal():
    marker = "test-only-marker-never-used-live"
    digest = sync.hashlib.sha256(marker.encode()).hexdigest()
    candidate = sync.build_workflow("credential-id", digest)
    serialized = json.dumps(candidate, sort_keys=True)
    assert marker not in serialized
    live = _live(candidate)
    result = sync.validate_live(live, candidate, marker)
    assert result["secret_literal_detected"] is False
    live["settings"]["secret_marker"] = marker
    with pytest.raises(sync.SyncError, match="workflow_secret_literal_detected"):
        sync.validate_live(live, candidate, marker)


def _run_query_auth_payload(secret: str, query) -> dict:
    digest = sync.hashlib.sha256(secret.encode()).hexdigest()
    payload = {"query": query, "body": {"network": "katalys"}}
    script = (
        f"const $input={{first:()=>({{json:{json.dumps(payload)}}})}};"
        "function run(){" + sync.query_auth_js(digest) + "}"
        "try{console.log(JSON.stringify({ok:true,value:run()}));}"
        "catch(error){console.log(JSON.stringify({ok:false,error:error.message}));}"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def _run_query_auth(secret: str, provided: str | None) -> dict:
    query = {} if provided is None else {sync.QUERY_SECRET_PARAMETER: provided}
    return _run_query_auth_payload(secret, query)


def test_query_secret_validates_at_edge_and_is_stripped_before_dispatch():
    result = _run_query_auth("valid-test-secret", "valid-test-secret")
    assert result["ok"] is True
    item = result["value"][0]["json"]
    assert set(item) == {"body", "query"}
    assert item["body"] == {"network": "katalys"}
    assert item["query"] == {}


def test_query_secret_preserves_only_allowlisted_everflow_identity():
    result = _run_query_auth_payload(
        "valid-test-secret",
        {
            "pb_key": "valid-test-secret",
            "network": "xentraffic",
            "offer_id": "3162",
            "click_id": "transaction-1",
            "conversion_id": "conversion-1",
            "event": "conversion",
        },
    )
    assert result["ok"] is True
    item = result["value"][0]["json"]
    assert item["body"] == {"network": "katalys"}
    assert item["query"] == {
        "network": "xentraffic",
        "offer_id": "3162",
        "click_id": "transaction-1",
        "conversion_id": "conversion-1",
        "event": "conversion",
    }
    assert "pb_key" not in item["query"]


@pytest.mark.parametrize(
    ("provided", "error"),
    [(None, "postback_auth_invalid"), ("wrong-secret", "postback_auth_invalid")],
)
def test_query_secret_missing_or_invalid_fails_before_dispatch(provided, error):
    result = _run_query_auth("valid-test-secret", provided)
    assert result == {"ok": False, "error": error}


@pytest.mark.parametrize(
    "query",
    [
        {"pb_key": "valid-test-secret", "unknown": "ambiguous"},
        {"pb_key": ["valid-test-secret", "second"]},
        {"pb_key": "valid-test-secret", "offer_id": ["3162", "3163"]},
        ["not-an-object"],
    ],
)
def test_query_secret_rejects_extra_array_and_ambiguous_shapes(query):
    result = _run_query_auth_payload("valid-test-secret", query)
    assert result == {"ok": False, "error": "postback_auth_invalid"}


def test_query_secret_never_appears_in_workflow_or_runtime_output():
    secret = "zero-secret-logging-test-value"
    digest = sync.hashlib.sha256(secret.encode()).hexdigest()
    candidate = sync.build_workflow("credential-id", digest)
    assert secret not in json.dumps(candidate, sort_keys=True)
    result = _run_query_auth(secret, secret)
    assert secret not in json.dumps(result, sort_keys=True)
    assert candidate["settings"]["saveDataSuccessExecution"] == "none"
    assert candidate["settings"]["saveDataErrorExecution"] == "none"


def test_validate_live_rejects_any_execution_persistence():
    candidate = sync.build_workflow("credential-id", "a" * 64)
    for field, reason in (
        ("saveDataSuccessExecution", "success_execution_persistence_enabled"),
        ("saveDataErrorExecution", "error_execution_persistence_enabled"),
    ):
        live = _live(candidate)
        live["settings"][field] = "all"
        with pytest.raises(sync.SyncError, match=reason):
            sync.validate_live(live, candidate, "not-the-live-secret")


def test_validate_live_rejects_stale_nested_active_version():
    candidate = sync.build_workflow("credential-id", "a" * 64)
    live = _live(candidate)
    live["activeVersion"]["connections"].pop("Query-Authenticated Postback GET")
    with pytest.raises(sync.SyncError, match="workflow_active_version_definition_drift"):
        sync.validate_live(live, candidate, "not-the-live-secret")


def test_validate_live_rejects_nested_active_version_identity_drift():
    candidate = sync.build_workflow("credential-id", "a" * 64)
    live = _live(candidate)
    live["activeVersion"]["id"] = "stale-version"
    with pytest.raises(sync.SyncError, match="workflow_active_version_definition_drift"):
        sync.validate_live(live, candidate, "not-the-live-secret")


def test_secret_bearing_n8n_error_body_is_hashed_never_reflected(monkeypatch):
    secret = "zero-secret-logging-provider-error-marker"
    body = json.dumps({"message": f"credential value rejected: {secret}"}).encode()
    error = urllib.error.HTTPError(
        "https://example.test/api/v1/credentials", 400, "bad request", {}, io.BytesIO(body)
    )
    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    client = object.__new__(sync.N8NClient)
    client.headers = {"Accept": "application/json", "Content-Type": "application/json"}
    with pytest.raises(sync.SyncError) as caught:
        client.request("POST", "/credentials", {"data": {"value": secret}})
    rendered = str(caught.value)
    assert secret not in rendered
    assert "credential value rejected" not in rendered
    assert "response_sha256=" in rendered


def test_secret_file_is_private_and_reused_without_rotation(tmp_path):
    path = tmp_path / "private" / "secret.txt"
    first, created = sync.ensure_secret(path)
    second, created_again = sync.ensure_secret(path)
    assert created is True
    assert created_again is False
    assert first == second
    assert len(first) >= 43
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_candidate_has_compatibility_path_plus_dedicated_query_get_and_no_portal_mutation_nodes():
    candidate = sync.build_workflow("credential-id")
    webhooks = [node for node in candidate["nodes"] if node["type"] == "n8n-nodes-base.webhook"]
    assert len(webhooks) == 3
    assert {node["parameters"]["path"] for node in webhooks} == {
        sync.WEBHOOK_PATH,
        sync.QUERY_WEBHOOK_PATH,
    }
    serialized = json.dumps(candidate).lower()
    assert "body.click.sub_id" in serialized
    assert "tracking" not in serialized
    assert "portal" not in serialized
    assert "sheet" not in serialized


def test_direct_cli_apply_refuses_before_n8n_client_construction(monkeypatch):
    constructed = False

    def forbidden_client():
        nonlocal constructed
        constructed = True
        raise AssertionError("n8n client must not be constructed")

    monkeypatch.setattr(sync, "N8NClient", forbidden_client)
    with pytest.raises(sync.SyncError, match="direct_receiver_workflow_write_authority_required"):
        sync.main(["--apply", "--repair-existing"])
    assert constructed is False


def _reviewed_fixture(monkeypatch, tmp_path, *, fail_post_write=False):
    secret = "s" * 64
    secret_path = tmp_path / "shared-secret.txt"
    secret_path.write_text(secret + "\n", encoding="utf-8")
    secret_path.chmod(0o600)
    monkeypatch.setattr(sync, "SECRET_PATH", secret_path)
    monkeypatch.setattr(sync, "ensure_secret", lambda: (secret, False))
    candidate = sync.build_workflow(
        "credential-id",
        sync.hashlib.sha256(secret.encode()).hexdigest(),
    )
    old_candidate = deepcopy(candidate)
    old_candidate["nodes"] = [
        node for node in old_candidate["nodes"]
        if node["name"] != "Query-Authenticated Postback GET"
    ]
    old_candidate["connections"].pop("Query-Authenticated Postback GET")
    before = _live(old_candidate)
    exact = _live(candidate)

    class Client:
        def __init__(self):
            self.current = before
            self.put_calls = 0
            self.credential_list_calls = 0
            self.fail_post_write = fail_post_write

        def list_credentials(self):
            self.credential_list_calls += 1
            return [{"id": "credential-id", "name": sync.CREDENTIAL_NAME}]

        def list_workflows(self):
            return [{"id": "workflow-id", "name": sync.WORKFLOW_NAME}]

        def get_workflow(self, workflow_id):
            assert workflow_id == "workflow-id"
            if self.put_calls and self.fail_post_write:
                raise sync.SyncError("simulated_readback_timeout")
            return deepcopy(self.current)

        def update_workflow(self, workflow_id, payload):
            assert workflow_id == "workflow-id"
            assert payload == candidate
            self.put_calls += 1
            self.current = exact
            return {"id": workflow_id}

        def create_credential(self, _secret):
            raise AssertionError("reviewed transition must not create credentials")

        def create_workflow(self, _candidate):
            raise AssertionError("reviewed transition must not create workflows")

        def activate_workflow(self, _workflow_id):
            raise AssertionError("reviewed transition must not activate separately")

    return (
        Client(),
        sync.canonical_sha256(sync.workflow_payload(before)),
        sync.canonical_sha256(candidate),
    )


@pytest.mark.parametrize("prerequisite", ["missing", "malformed", "permissions", "destination", "valid"])
def test_cli_receipt_prerequisites_precede_any_provider_mutation(monkeypatch, tmp_path, prerequisite):
    client, before_sha, after_sha = _reviewed_fixture(monkeypatch, tmp_path)
    key_path = tmp_path / "authority-key"
    if prerequisite != "missing":
        key_path.write_bytes(b"short" if prerequisite == "malformed" else b"k" * 64)
        key_path.chmod(0o644 if prerequisite == "permissions" else 0o600)
    real_attest = sync.authority.attest_receipt
    monkeypatch.setattr(sync.authority, "attest_receipt", lambda value: real_attest(value, key_path=key_path))
    monkeypatch.setattr(sync, "N8NClient", lambda: client)
    receipt = tmp_path / "receipt.json"
    if prerequisite == "destination":
        receipt.mkdir()
    argv = ["--apply", "--repair-existing", "--expected-before-sha256", before_sha,
            "--expected-after-sha256", after_sha, "--receipt", str(receipt)]
    if prerequisite == "valid":
        assert sync.main(argv) == 0
        assert client.put_calls == 1
        saved = json.loads(receipt.read_text())
        assert sync.authority.verify_receipt_attestation(saved, key_path=key_path) == (True, "")
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    else:
        with pytest.raises(sync.SyncError):
            sync.main(argv)
        assert client.put_calls == 0
        assert client.credential_list_calls == 0


def test_reviewed_transition_performs_one_put_and_exact_active_readback(monkeypatch, tmp_path):
    client, before_sha, after_sha = _reviewed_fixture(monkeypatch, tmp_path)
    result = sync.apply_live(
        client,
        repair_existing=True,
        expected_before_sha256=before_sha,
        expected_after_sha256=after_sha,
    )
    assert client.put_calls == 1
    assert result["write_performed"] is True
    assert result["exact_transition_enforced"] is True
    assert result["before_definition_sha256"] == before_sha
    assert result["after_definition_sha256"] == after_sha
    assert result["live"]["definition_sha256"] == after_sha
    assert result["live"]["active_executable_sha256"] == sync.canonical_sha256(
        sync.executable_payload(sync.build_workflow(
            "credential-id", sync.hashlib.sha256(("s" * 64).encode()).hexdigest()
        ))
    )


def test_reviewed_transition_rejects_stale_before_without_put(monkeypatch, tmp_path):
    client, _before_sha, after_sha = _reviewed_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sync,
        "read_existing_secret",
        lambda _path: pytest.fail("secret read before stale provider preimage rejection"),
    )
    with pytest.raises(sync.SyncError, match="reviewed_before_definition_sha256_mismatch"):
        sync.apply_live(
            client,
            repair_existing=True,
            expected_before_sha256="0" * 64,
            expected_after_sha256=after_sha,
        )
    assert client.put_calls == 0
    assert client.credential_list_calls == 0


def test_reviewed_transition_rejects_wrong_after_without_put(monkeypatch, tmp_path):
    client, before_sha, _after_sha = _reviewed_fixture(monkeypatch, tmp_path)
    with pytest.raises(sync.SyncError, match="reviewed_after_definition_sha256_mismatch"):
        sync.apply_live(
            client,
            repair_existing=True,
            expected_before_sha256=before_sha,
            expected_after_sha256="0" * 64,
        )
    assert client.put_calls == 0


def test_reviewed_transition_post_write_uncertainty_never_rolls_back_or_refires(
    monkeypatch, tmp_path,
):
    client, before_sha, after_sha = _reviewed_fixture(
        monkeypatch, tmp_path, fail_post_write=True,
    )
    with pytest.raises(sync.SyncError, match="post_write_reconcile_without_refire"):
        sync.apply_live(
            client,
            repair_existing=True,
            expected_before_sha256=before_sha,
            expected_after_sha256=after_sha,
        )
    assert client.put_calls == 1


def test_reviewed_cli_writes_private_attested_receipt_and_safe_stdout(
    monkeypatch, tmp_path, capsys,
):
    client, before_sha, after_sha = _reviewed_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(sync, "N8NClient", lambda: client)
    monkeypatch.setattr(
        sync.authority,
        "attest_receipt",
        lambda receipt: {**receipt, "authority_attestation": {"schema": "test", "hmac_sha256": "a" * 64}},
    )
    receipt = tmp_path / "receipt.json"
    assert sync.main([
        "--apply",
        "--repair-existing",
        "--expected-before-sha256", before_sha,
        "--expected-after-sha256", after_sha,
        "--receipt", str(receipt),
    ]) == 0
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert saved["authority_attestation"]["hmac_sha256"] == "a" * 64
    stdout = capsys.readouterr().out
    assert "credential-id" not in stdout
    assert sync.QUERY_WEBHOOK_URL not in stdout
    assert "nodes" not in stdout


@pytest.mark.parametrize("missing", ["Run Durable Ledger Writer", "Build Sanitized Bridge Request"])
def test_equal_saved_and_published_graphs_cannot_hide_a_missing_referenced_node(missing):
    candidate = sync.build_workflow("credential-id", "a" * 64)
    candidate["nodes"] = [n for n in candidate["nodes"] if n["name"] != missing]
    with pytest.raises(sync.SyncError, match="workflow_connection_node_missing"):
        sync.validate_live(_live(candidate), candidate, "")


def test_query_identity_repair_is_minimal_and_authenticates_maxbounty_and_test():
    import hashlib
    secret = "synthetic-only-regression-secret" * 3
    digest = hashlib.sha256(secret.encode()).hexdigest()
    before = _live(sync.build_workflow("credential-id", digest))
    before["id"] = "dG2y4DqpoHEMCLfD"
    before["settings"]["availableInMCP"] = True
    next(n for n in before["nodes"] if n["name"] == "Run Durable Ledger Writer")["retryOnFail"] = False
    before["activeVersion"]["nodes"] = deepcopy(before["nodes"])
    candidate = sync.build_query_identity_repair(before, digest)
    restored = deepcopy(candidate)
    next(n for n in restored["nodes"] if n["name"] == "Verify Query Secret and Strip")["parameters"]["jsCode"] = sync.query_auth_js(digest)
    assert restored == sync.workflow_payload(before)
    code = next(n for n in candidate["nodes"] if n["name"] == "Verify Query Secret and Strip")["parameters"]["jsCode"]
    query = {"pb_key":secret,"network":"maxbounty","offer_id":"32122","network_account_ref":"803003","click_id":"TEST-CLICK","conversion_id":"TEST-CONVERSION","event":"conversion","event_status":"test","is_test":"true"}
    program = "const d=JSON.parse(require('fs').readFileSync(0,'utf8')); try {const result=new Function('$input',d.code)({first:()=>({json:{query:d.query,headers:{ip:'forbidden'}}})}); console.log(JSON.stringify({ok:true,result}));} catch(e) {console.log(JSON.stringify({ok:false,reason:e.message}));}"
    def run(q):
        return json.loads(subprocess.run(["node","-e",program],input=json.dumps({"code":code,"query":q}),text=True,capture_output=True,check=True).stdout)
    result = run(query)
    assert result["ok"] is True
    assert result["result"][0]["json"] == {"body":{},"query":{k:v for k,v in query.items() if k != "pb_key"}}
    for extra in [{"ip":"192.0.2.1"},{"email":"test@example.invalid"},{"diagnosis":"forbidden"},{"commission":"1"},{"pb_key":"wrong"}]:
        assert run({**query,**extra})["ok"] is False
    candidate_live = _live(candidate)
    candidate_live["id"] = before["id"]
    assert sync.build_query_identity_repair(candidate_live,digest) == candidate
    assert sync.validate_live(candidate_live,candidate,secret)["secret_literal_detected"] is False


def test_scoped_repair_refuses_unknown_authenticator_without_changing_input():
    before = _live(sync.build_workflow("credential-id", "a" * 64))
    before["id"] = "dG2y4DqpoHEMCLfD"
    next(n for n in before["nodes"] if n["name"] == "Verify Query Secret and Strip")["parameters"]["jsCode"] += "\n// unreviewed drift"
    before["activeVersion"]["nodes"] = deepcopy(before["nodes"])
    original = deepcopy(before)
    with pytest.raises(sync.SyncError, match="query_authenticator_predecessor_drift"):
        sync.build_query_identity_repair(before, "a" * 64)
    assert before == original


def test_scoped_exact_transition_preserves_current_graph_and_never_provisions(monkeypatch, tmp_path):
    secret = "s" * 64
    secret_path = tmp_path / "shared-secret.txt"
    secret_path.write_text(secret)
    secret_path.chmod(0o600)
    monkeypatch.setattr(sync, "SECRET_PATH", secret_path)
    before = _live(sync.build_workflow("credential-id", sync.hashlib.sha256(secret.encode()).hexdigest()))
    before["id"] = "dG2y4DqpoHEMCLfD"
    next(n for n in before["nodes"] if n["name"] == "Run Durable Ledger Writer")["retryOnFail"] = False
    before["activeVersion"]["nodes"] = deepcopy(before["nodes"])
    candidate = sync.build_query_identity_repair(before, sync.hashlib.sha256(secret.encode()).hexdigest())
    class Client:
        current = deepcopy(before)
        puts = 0
        def list_workflows(self): return [deepcopy(self.current)]
        def get_workflow(self, wid):
            assert wid == before["id"]
            return deepcopy(self.current)
        def update_workflow(self, wid, payload):
            assert wid == before["id"] and payload == candidate
            self.puts += 1
            self.current = _live(payload)
            self.current["id"] = wid
        def __getattr__(self, name):
            raise AssertionError("unexpected provisioning/activation: " + name)
    client = Client()
    result = sync.apply_live(client, repair_existing=True, repair_query_identity=True,
        expected_before_sha256=sync.canonical_sha256(sync.workflow_payload(before)),
        expected_after_sha256=sync.canonical_sha256(candidate))
    assert client.puts == 1
    assert result["credential_created"] is False and result["secret_created"] is False
    assert result["workflow_created"] is False and result["exact_transition_enforced"] is True
    with pytest.raises(sync.SyncError, match="requires_exact_transition"):
        sync.apply_live(client, repair_existing=True, repair_query_identity=True)
    assert client.puts == 1
