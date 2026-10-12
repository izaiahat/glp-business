#!/usr/bin/env python3
"""Provision and verify the registered n8n GLP affiliate postback receiver.

The script uses n8n's public API only after the MCP quota gate has been proven.
It creates one dedicated Header Auth credential, one workflow, and activates the
workflow. Secret values are generated locally, stored 0600, submitted only in
the credential-create request, and never printed or embedded in workflow JSON.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import glp_postback_authority as authority  # noqa: E402

N8N_BASE = "https://hermesops.app.n8n.cloud"
API_KEY_PATH = Path("/home/ubuntu/.hermes/secrets/n8n/native-api-key.txt")
SECRET_PATH = Path("/home/ubuntu/.hermes/secrets/glp-affiliate-postback/shared-secret.txt")
WORKFLOW_NAME = "glp/affiliate-postback-receiver"
WEBHOOK_PATH = "glp-affiliate-postback"
WEBHOOK_URL = f"{N8N_BASE}/webhook/{WEBHOOK_PATH}"
QUERY_WEBHOOK_PATH = WEBHOOK_PATH + "-query"
QUERY_WEBHOOK_URL = f"{N8N_BASE}/webhook/{QUERY_WEBHOOK_PATH}"
WEBHOOK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, WEBHOOK_URL))
POST_WEBHOOK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, WEBHOOK_URL + ":POST"))
QUERY_GET_WEBHOOK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, QUERY_WEBHOOK_URL + ":GET"))
CREDENTIAL_NAME = "glp-affiliate-postback-header"
HEADER_NAME = "X-GLP-Postback-Secret"
QUERY_SECRET_PARAMETER = "pb_key"
BRIDGE_URL = "https://bridge.allocant.ai/run"
BRIDGE_CREDENTIAL = {"id": "okIc23U3bwTzODug", "name": "bridge-auth"}
RECEIPT_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/postback-receiver"


class SyncError(RuntimeError):
    """Fail-closed n8n provisioning error."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def ensure_secret(path: Path = SECRET_PATH) -> tuple[str, bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    created = False
    if not path.exists():
        value = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        created = True
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise SyncError("postback_secret_permissions_invalid")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 43:
        raise SyncError("postback_secret_entropy_invalid")
    return value, created


def read_existing_secret(path: Path) -> str:
    """Read reviewed secret material without mkdir, chmod, or rotation."""
    if not path.is_file():
        raise SyncError("reviewed_postback_secret_missing")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SyncError("postback_secret_permissions_invalid")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 43:
        raise SyncError("postback_secret_entropy_invalid")
    return value


def validate_existing_receiver_material(
    receipt: dict[str, Any],
    shared_secret: str,
    *,
    authority_key_path: Path = authority.AUTHORITY_KEY_PATH,
    secret_path: Path = SECRET_PATH,
) -> dict[str, Any]:
    """Bind existing secret bytes to the attested active receiver executable.

    This validator is read-only. It rebuilds the deterministic receiver candidate
    from the receipt's existing credential identity and the SHA-256 of the
    already-present secret, then requires both the full definition and nested
    active executable hashes to match the reviewed exact-transition receipt.
    """
    if not isinstance(receipt, dict):
        raise SyncError("reviewed_receiver_receipt_invalid")
    try:
        attested, attestation_reason = authority.verify_receipt_attestation(
            receipt, key_path=authority_key_path
        )
    except authority.AuthorityError as exc:
        raise SyncError("reviewed_receiver_attestation_invalid") from exc
    if not attested or attestation_reason:
        raise SyncError("reviewed_receiver_attestation_invalid")

    live = receipt.get("live")
    credential_id = str(receipt.get("credential_id") or "")
    if not isinstance(live, dict) or not credential_id:
        raise SyncError("reviewed_receiver_receipt_invalid")
    if len(shared_secret) < 43:
        raise SyncError("postback_secret_entropy_invalid")

    secret_sha256 = hashlib.sha256(shared_secret.encode("utf-8")).hexdigest()
    candidate = build_workflow(credential_id, secret_sha256)
    definition_sha256 = canonical_sha256(candidate)
    executable_sha256 = canonical_sha256(executable_payload(candidate))
    before_sha256 = str(receipt.get("before_definition_sha256") or "")
    active_version_id = str(live.get("active_version_id") or "")
    serialized = json.dumps(receipt, sort_keys=True)
    if (
        receipt.get("schema") != "glp-affiliate-postback-n8n-sync-v1"
        or receipt.get("status") != "PASS"
        or receipt.get("write_performed") is not True
        or receipt.get("workflow_created") is not False
        or receipt.get("workflow_updated") is not True
        or receipt.get("credential_created") is not False
        or receipt.get("secret_created") is not False
        or receipt.get("secret_path") != str(secret_path)
        or receipt.get("secret_permissions") != "0600"
        or receipt.get("credential_name") != CREDENTIAL_NAME
        or receipt.get("exact_transition_enforced") is not True
        or not re.fullmatch(r"[a-f0-9]{64}", before_sha256)
        or before_sha256 == definition_sha256
        or receipt.get("after_definition_sha256") != definition_sha256
        or receipt.get("expected_after_definition_sha256") != definition_sha256
        or receipt.get("expected_before_definition_sha256") != before_sha256
        or receipt.get("secrets_printed") is not False
        or live.get("name") != WORKFLOW_NAME
        or live.get("active") is not True
        or not str(live.get("id") or "")
        or not active_version_id
        or str(live.get("version_id") or "") != active_version_id
        or str(live.get("nested_active_version_id") or "") != active_version_id
        or live.get("definition_sha256") != definition_sha256
        or live.get("candidate_sha256") != definition_sha256
        or live.get("active_executable_sha256") != executable_sha256
        or live.get("webhook_url") != WEBHOOK_URL
        or live.get("query_webhook_url") != QUERY_WEBHOOK_URL
        or live.get("webhook_authentication") != "headerAuth+querySha256"
        or live.get("accepted_authentication_modes") != ["shared_secret"]
        or live.get("accepted_authentication_placements") != ["header", "query"]
        or live.get("query_secret_parameter") != QUERY_SECRET_PARAMETER
        or set(live.get("webhook_methods") or []) != {"GET", "POST"}
        or live.get("execution_data_saved_success") != "none"
        or live.get("execution_data_saved_error") != "none"
        or live.get("secret_literal_detected") is not False
        or shared_secret in serialized
    ):
        raise SyncError("reviewed_receiver_secret_executable_mismatch")
    return {
        "status": "PASS_REVIEWED_EXISTING_RECEIVER_MATERIAL",
        "receipt_attested": True,
        "definition_sha256": definition_sha256,
        "active_executable_sha256": executable_sha256,
        "secret_sha256_matches_active_executable": True,
        "receiver_writes_performed": 0,
    }


def query_auth_js(secret_sha256: str) -> str:
    """Return the no-dependency first-node query authenticator.

    Only the SHA-256 digest of the high-entropy shared secret is embedded in the
    workflow. The caller's query value is removed before the item can reach the
    normalizer, bridge request, execution receipt, or durable ledger logic.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", secret_sha256):
        raise SyncError("query_secret_sha256_invalid")
    return r"""function sha256(ascii) {
  function rightRotate(value, amount) { return (value >>> amount) | (value << (32 - amount)); }
  const mathPow = Math.pow;
  const maxWord = mathPow(2, 32);
  let result = '';
  const words = [];
  const asciiBitLength = ascii.length * 8;
  let hash = sha256.h = sha256.h || [];
  let k = sha256.k = sha256.k || [];
  let primeCounter = k.length;
  const isComposite = {};
  for (let candidate = 2; primeCounter < 64; candidate++) {
    if (!isComposite[candidate]) {
      for (let i = 0; i < 313; i += candidate) isComposite[i] = candidate;
      hash[primeCounter] = (mathPow(candidate, 0.5) * maxWord) | 0;
      k[primeCounter++] = (mathPow(candidate, 1 / 3) * maxWord) | 0;
    }
  }
  ascii += '\x80';
  while (ascii.length % 64 - 56) ascii += '\x00';
  for (let i = 0; i < ascii.length; i++) {
    const code = ascii.charCodeAt(i);
    if (code >> 8) throw new Error('postback_auth_encoding_invalid');
    words[i >> 2] |= code << ((3 - i) % 4) * 8;
  }
  words[words.length] = (asciiBitLength / maxWord) | 0;
  words[words.length] = asciiBitLength;
  for (let j = 0; j < words.length;) {
    const w = words.slice(j, j += 16);
    const oldHash = hash.slice(0);
    for (let i = 0; i < 64; i++) {
      const w15 = w[i - 15], w2 = w[i - 2];
      const a = hash[0], e = hash[4];
      const temp1 = hash[7]
        + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
        + ((e & hash[5]) ^ ((~e) & hash[6])) + k[i]
        + (w[i] = i < 16 ? w[i] : (w[i - 16]
          + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3))
          + w[i - 7]
          + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))) | 0);
      const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
        + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
      hash = [(temp1 + temp2) | 0].concat(hash);
      hash[4] = (hash[4] + temp1) | 0;
      hash.pop();
    }
    for (let i = 0; i < 8; i++) hash[i] = (hash[i] + oldHash[i]) | 0;
  }
  for (let i = 0; i < 8; i++) {
    for (let j = 3; j + 1; j--) {
      const b = (hash[i] >> (j * 8)) & 255;
      result += (b < 16 ? '0' : '') + b.toString(16);
    }
  }
  return result;
}
const input = $input.first().json;
const query = (input.query && typeof input.query === 'object' && !Array.isArray(input.query))
  ? input.query : null;
const allowedQueryKeys = new Set([
  'pb_key', 'network', 'offer_id', 'click_id', 'conversion_id', 'event'
]);
if (!query || !Object.prototype.hasOwnProperty.call(query, 'pb_key')) {
  throw new Error('postback_auth_invalid');
}
for (const [key, value] of Object.entries(query)) {
  if (!allowedQueryKeys.has(key)) throw new Error('postback_auth_invalid');
  if (typeof value !== 'string' || !value.trim()) throw new Error('postback_auth_invalid');
}
const provided = query.pb_key;
if (typeof provided !== 'string' || !provided) throw new Error('postback_auth_invalid');
const actual = sha256(provided);
const expected = '__QUERY_SECRET_SHA256__';
let mismatch = actual.length ^ expected.length;
for (let i = 0; i < Math.max(actual.length, expected.length); i++) {
  mismatch |= (actual.charCodeAt(i) || 0) ^ (expected.charCodeAt(i) || 0);
}
if (mismatch !== 0) throw new Error('postback_auth_invalid');
const body = (input.body && typeof input.body === 'object' && !Array.isArray(input.body))
  ? input.body : {};
const cleanQuery = {};
for (const [key, value] of Object.entries(query)) {
  if (key !== 'pb_key') cleanQuery[key] = value.trim();
}
return [{json: {body, query: cleanQuery}}];""".replace("__QUERY_SECRET_SHA256__", secret_sha256)


NORMALIZE_JS = r"""const input = $input.first().json;
const query = (input.query && typeof input.query === 'object') ? input.query : {};
const body = (input.body && typeof input.body === 'object') ? input.body : {};
const source = {...body, ...query};
if (!source.click_id && body.click && typeof body.click === 'object') source.click_id = body.click.sub_id;
if (!source.conversion_id) source.conversion_id = body.id || body.conversion_id;
const allowed = ['network','offer_id','click_id','conversion_id','event','event_status','currency','order_amount','commission','occurred_at','network_account_ref','order_ref','is_test'];
const clean = {};
for (const key of allowed) {
  if (source[key] !== undefined && source[key] !== null && String(source[key]).trim() !== '') {
    clean[key] = String(source[key]).trim();
  }
}
for (const key of ['network','offer_id','click_id','conversion_id']) {
  if (!clean[key]) throw new Error(`required_field_missing:${key}`);
}
clean.event = clean.event || 'conversion';
clean.event_status = clean.event_status || 'approved';
clean.currency = clean.currency || 'USD';
return [{json: clean}];"""

PARSE_JS = r"""const response = $input.first().json;
const statusCode = Number(response.statusCode || 200);
const bridge = response.body || response;
if (statusCode >= 400) {
  throw new Error(`postback_bridge_http_${statusCode}:${bridge.error || 'unknown'}`);
}
if (bridge.status !== 'done' || bridge.rc !== 0 || !bridge.stdout_tail) {
  throw new Error(`postback_writer_failed:${bridge.status || 'unknown'}:${bridge.rc ?? 'none'}`);
}
let receipt;
try { receipt = JSON.parse(String(bridge.stdout_tail).trim()); }
catch (_) { throw new Error('postback_writer_receipt_invalid'); }
if (!String(receipt.status || '').startsWith('PASS_')) {
  throw new Error(`postback_writer_fail_closed:${receipt.reason || 'unknown'}`);
}
return [{json: receipt}];"""

BUILD_REQUEST_JS = r"""const payload = $input.first().json;
return [{json:{
  action:'script',
  name:'affiliate_postback_receive',
  args:{
    'auth-context':'n8n_authenticated_edge_v2',
    'payload-json':JSON.stringify(payload),
    'target':'remote'
  }
}}];"""


def build_workflow(credential_id: str, query_secret_sha256: str = "0" * 64) -> dict[str, Any]:
    if not credential_id:
        raise SyncError("header_credential_id_required")
    nodes = [
        {
            "id": "postback-webhook",
            "name": "Authenticated Postback",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [-620, 0],
            "webhookId": WEBHOOK_ID,
            "parameters": {
                "httpMethod": "GET",
                "path": WEBHOOK_PATH,
                "authentication": "headerAuth",
                "responseMode": "lastNode",
                "responseData": "firstEntryJson",
                "options": {},
            },
            "credentials": {
                "httpHeaderAuth": {"id": credential_id, "name": CREDENTIAL_NAME}
            },
            "notes": (
                "Dedicated Header Auth rejects missing/wrong shared secret before execution. "
                "Success/error execution data is disabled so authenticated headers are not persisted."
            ),
            "notesInFlow": True,
        },
        {
            "id": "postback-webhook-post",
            "name": "Query-Authenticated Postback POST",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [-620, 220],
            "webhookId": POST_WEBHOOK_ID,
            "parameters": {
                "httpMethod": "POST",
                "path": WEBHOOK_PATH,
                "authentication": "none",
                "responseMode": "lastNode",
                "responseData": "firstEntryJson",
                "options": {},
            },
            "notes": (
                "Same public receiver URL for partners that cannot send a custom header. "
                "The first connected node validates the query secret by SHA-256, strips it, "
                "and only then allows normalization. Execution persistence is disabled."
            ),
            "notesInFlow": True,
        },
        {
            "id": "postback-webhook-query-get",
            "name": "Query-Authenticated Postback GET",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [-620, 440],
            "webhookId": QUERY_GET_WEBHOOK_ID,
            "parameters": {
                "httpMethod": "GET",
                "path": QUERY_WEBHOOK_PATH,
                "authentication": "none",
                "responseMode": "lastNode",
                "responseData": "firstEntryJson",
                "options": {},
            },
            "notes": (
                "Dedicated query-authenticated GET receiver for partners that deliver conversion "
                "identity only in URL parameters. The first connected node validates and strips "
                "the shared secret before normalization. Execution persistence is disabled."
            ),
            "notesInFlow": True,
        },
        {
            "id": "verify-query-secret",
            "name": "Verify Query Secret and Strip",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [-440, 220],
            "parameters": {"jsCode": query_auth_js(query_secret_sha256)},
            "notes": (
                "Validates before durable receiver logic. The secret value is never embedded, "
                "persisted, dispatched, or returned; only its SHA-256 verifier is present."
            ),
            "notesInFlow": True,
        },
        {
            "id": "normalize-postback",
            "name": "Strip Headers and Normalize Identity",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [-360, 0],
            "parameters": {"jsCode": NORMALIZE_JS},
            "notes": "Returns only allowlisted conversion identity fields. No headers or secrets leave this node.",
            "notesInFlow": True,
        },
        {
            "id": "build-bridge-request",
            "name": "Build Sanitized Bridge Request",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [-80, 0],
            "parameters": {"jsCode": BUILD_REQUEST_JS},
            "notes": "Wraps only the normalized identity item in the allowlisted bridge action contract.",
            "notesInFlow": True,
        },
        {
            "id": "run-ledger-writer",
            "name": "Run Durable Ledger Writer",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [200, 0],
            "parameters": {
                "method": "POST",
                "url": BRIDGE_URL,
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendBody": True,
                "contentType": "json",
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json) }}",
                "options": {
                    "timeout": 20000,
                    "response": {
                        "response": {
                            "fullResponse": True,
                            "neverError": True,
                            "responseFormat": "json",
                        }
                    },
                },
            },
            "credentials": {"httpHeaderAuth": dict(BRIDGE_CREDENTIAL)},
            "retryOnFail": True,
            "maxTries": 5,
            "waitBetweenTries": 4000,
            "notes": "Registered bridge action. Payload is identity-only; D1 INSERT OR IGNORE plus readback is the effect gate.",
            "notesInFlow": True,
        },
        {
            "id": "parse-ledger-receipt",
            "name": "Return Deterministic Receipt",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [480, 0],
            "parameters": {"jsCode": PARSE_JS},
            "notes": "Returns only the deterministic D1 readback receipt. Bridge errors fail closed.",
            "notesInFlow": True,
        },
    ]
    connections = {
        "Authenticated Postback": {
            "main": [[{"node": "Strip Headers and Normalize Identity", "type": "main", "index": 0}]]
        },
        "Query-Authenticated Postback POST": {
            "main": [[{"node": "Verify Query Secret and Strip", "type": "main", "index": 0}]]
        },
        "Query-Authenticated Postback GET": {
            "main": [[{"node": "Verify Query Secret and Strip", "type": "main", "index": 0}]]
        },
        "Verify Query Secret and Strip": {
            "main": [[{"node": "Strip Headers and Normalize Identity", "type": "main", "index": 0}]]
        },
        "Strip Headers and Normalize Identity": {
            "main": [[{"node": "Build Sanitized Bridge Request", "type": "main", "index": 0}]]
        },
        "Build Sanitized Bridge Request": {
            "main": [[{"node": "Run Durable Ledger Writer", "type": "main", "index": 0}]]
        },
        "Run Durable Ledger Writer": {
            "main": [[{"node": "Return Deterministic Receipt", "type": "main", "index": 0}]]
        },
    }
    return {
        "name": WORKFLOW_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
            "timezone": "America/Los_Angeles",
            "executionTimeout": 30,
            "saveDataSuccessExecution": "none",
            "saveDataErrorExecution": "none",
            "saveManualExecutions": False,
            "saveExecutionProgress": False,
        },
    }


class N8NClient:
    def __init__(self, key_path: Path = API_KEY_PATH) -> None:
        key = key_path.read_text(encoding="utf-8").strip()
        if not key:
            raise SyncError("n8n_api_key_missing")
        self.headers = {
            "X-N8N-API-KEY": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            N8N_BASE + "/api/v1" + path,
            data=raw_body,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            # Credential-create errors may echo submitted values. Never surface
            # provider text from a secret-bearing request; a bounded reason code
            # plus response hash is enough to reconcile the failure safely.
            raise SyncError(
                f"n8n_api_{method}_{path}_http_{exc.code}:"
                f"response_sha256={hashlib.sha256(raw).hexdigest()}"
            ) from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise SyncError("n8n_api_invalid_json") from exc
        if not isinstance(result, dict):
            raise SyncError("n8n_api_non_object")
        return result

    def list_credentials(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/credentials?limit=100").get("data") or [])

    def create_credential(self, shared_secret: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/credentials",
            {
                "name": CREDENTIAL_NAME,
                "type": "httpHeaderAuth",
                "data": {"name": HEADER_NAME, "value": shared_secret},
            },
        )

    def list_workflows(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/workflows?limit=100").get("data") or [])

    def create_workflow(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/workflows", candidate)

    def update_workflow(self, workflow_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "PUT", f"/workflows/{workflow_id}?publishIfActive=true", candidate
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.request("GET", f"/workflows/{workflow_id}")

    def activate_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.request("POST", f"/workflows/{workflow_id}/activate")


def workflow_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": workflow.get("name"),
        "nodes": workflow.get("nodes"),
        "connections": workflow.get("connections"),
        "settings": workflow.get("settings"),
    }


def executable_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": workflow.get("nodes") or [],
        "connections": workflow.get("connections") or {},
    }


def validate_graph_references(workflow: dict[str, Any]) -> None:
    """Parity alone cannot establish graph completeness."""
    nodes, connections = workflow.get("nodes"), workflow.get("connections")
    if not isinstance(nodes, list) or not isinstance(connections, dict):
        raise SyncError("workflow_graph_body_invalid")
    names = [node.get("name") for node in nodes if isinstance(node, dict)]
    if len(names) != len(nodes) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise SyncError("workflow_node_identity_invalid")
    names = set(names)
    for source, outputs in connections.items():
        if source not in names:
            raise SyncError("workflow_connection_node_missing")
        if not isinstance(outputs, dict):
            raise SyncError("workflow_connections_invalid")
        for ports in outputs.values():
            if not isinstance(ports, list):
                raise SyncError("workflow_connections_invalid")
            for edges in ports:
                if not isinstance(edges, list):
                    raise SyncError("workflow_connections_invalid")
                for edge in edges:
                    if not isinstance(edge, dict) or edge.get("node") not in names:
                        raise SyncError("workflow_connection_node_missing")


def build_query_identity_repair(live: dict[str, Any], secret_sha256: str) -> dict[str, Any]:
    """Only extend the existing query edge, never rebuild its current graph.

    Preserve the historical build_workflow()/receipt contract. The query edge
    gains the existing downstream account identity and TEST metadata only;
    payout, IP, PHI and arbitrary keys remain rejected.
    """
    if live.get("id") != "dG2y4DqpoHEMCLfD":
        raise SyncError("query_identity_repair_workflow_identity_invalid")
    candidate = deepcopy(workflow_payload(live))
    validate_live(live, candidate, "")
    original = query_auth_js(secret_sha256)
    repaired = original.replace(
        "'pb_key', 'network', 'offer_id', 'click_id', 'conversion_id', 'event'",
        "'pb_key', 'network', 'offer_id', 'click_id', 'conversion_id', 'event', "
        "'network_account_ref', 'event_status', 'is_test'",
    )
    verifiers = [n for n in candidate["nodes"] if n["name"] == "Verify Query Secret and Strip"]
    if len(verifiers) != 1 or verifiers[0].get("type") != "n8n-nodes-base.code":
        raise SyncError("query_authenticator_predecessor_drift")
    parameters = verifiers[0].get("parameters") or {}
    if parameters.get("jsCode") not in (original, repaired):
        raise SyncError("query_authenticator_predecessor_drift")
    parameters["jsCode"] = repaired
    return candidate


def validate_live(
    live: dict[str, Any], candidate: dict[str, Any], shared_secret: str
) -> dict[str, Any]:
    validate_graph_references(candidate)
    validate_graph_references(live)
    if live.get("name") != WORKFLOW_NAME:
        raise SyncError("workflow_name_drift")
    if live.get("active") is not True:
        raise SyncError("workflow_not_active")
    active_version = live.get("activeVersion")
    if not isinstance(active_version, dict):
        raise SyncError("workflow_active_version_missing")
    validate_graph_references(active_version)
    active_version_id = str(live.get("activeVersionId") or "")
    nested_active_version_id = str(
        active_version.get("id") or active_version.get("versionId") or ""
    )
    if (
        not live.get("versionId")
        or str(live.get("versionId")) != active_version_id
        or not active_version_id
        or nested_active_version_id != active_version_id
        or canonical_sha256(executable_payload(active_version))
        != canonical_sha256(executable_payload(candidate))
    ):
        raise SyncError("workflow_active_version_definition_drift")
    nodes = live.get("nodes") or []
    webhooks = [node for node in nodes if node.get("type") == "n8n-nodes-base.webhook"]
    if len(webhooks) != 3:
        raise SyncError("workflow_webhook_count_invalid")
    by_edge = {
        ((node.get("parameters") or {}).get("path"),
         (node.get("parameters") or {}).get("httpMethod")): node
        for node in webhooks
    }
    expected_edges = {
        (WEBHOOK_PATH, "GET"),
        (WEBHOOK_PATH, "POST"),
        (QUERY_WEBHOOK_PATH, "GET"),
    }
    if set(by_edge) != expected_edges:
        raise SyncError("workflow_webhook_auth_or_path_invalid")
    if (by_edge[(WEBHOOK_PATH, "GET")].get("parameters") or {}).get("authentication") != "headerAuth":
        raise SyncError("workflow_header_auth_missing")
    for edge in ((WEBHOOK_PATH, "POST"), (QUERY_WEBHOOK_PATH, "GET")):
        if (by_edge[edge].get("parameters") or {}).get("authentication") != "none":
            raise SyncError("workflow_query_auth_edge_not_first_node")
    connections = live.get("connections") or {}
    for webhook_name in (
        "Query-Authenticated Postback POST",
        "Query-Authenticated Postback GET",
    ):
        first = (((connections.get(webhook_name) or {}).get("main")
                  or [[{}]])[0] or [{}])[0]
        if first.get("node") != "Verify Query Secret and Strip":
            raise SyncError("workflow_query_auth_edge_not_first_node")
    settings = live.get("settings") or {}
    if settings.get("saveDataSuccessExecution") != "none":
        raise SyncError("workflow_success_execution_persistence_enabled")
    if settings.get("saveDataErrorExecution") != "none":
        raise SyncError("workflow_error_execution_persistence_enabled")
    if settings.get("saveManualExecutions") is not False:
        raise SyncError("workflow_manual_execution_persistence_enabled")
    if settings.get("saveExecutionProgress") is not False:
        raise SyncError("workflow_execution_progress_persistence_enabled")
    serialized = json.dumps(workflow_payload(live), sort_keys=True)
    if shared_secret and shared_secret in serialized:
        raise SyncError("workflow_secret_literal_detected")
    if canonical_sha256(workflow_payload(live)) != canonical_sha256(candidate):
        raise SyncError("workflow_top_level_definition_drift")
    return {
        "id": live.get("id"),
        "name": live.get("name"),
        "active": live.get("active"),
        "version_id": live.get("versionId"),
        "active_version_id": active_version_id,
        "nested_active_version_id": nested_active_version_id,
        "active_executable_sha256": canonical_sha256(executable_payload(active_version)),
        "definition_sha256": canonical_sha256(workflow_payload(live)),
        "candidate_sha256": canonical_sha256(candidate),
        "webhook_url": WEBHOOK_URL,
        "query_webhook_url": QUERY_WEBHOOK_URL,
        "webhook_authentication": "headerAuth+querySha256",
        "accepted_authentication_modes": ["shared_secret"],
        "accepted_authentication_placements": ["header", "query"],
        "query_secret_parameter": QUERY_SECRET_PARAMETER,
        "webhook_methods": ["GET", "POST"],
        "execution_data_saved_success": settings.get("saveDataSuccessExecution"),
        "execution_data_saved_error": settings.get("saveDataErrorExecution"),
        "secret_literal_detected": False,
    }


def _reviewed_transition(
    expected_before_sha256: str | None,
    expected_after_sha256: str | None,
) -> bool:
    if bool(expected_before_sha256) != bool(expected_after_sha256):
        raise SyncError("reviewed_transition_hash_pair_required")
    for label, value in (
        ("before", expected_before_sha256),
        ("after", expected_after_sha256),
    ):
        if value and not re.fullmatch(r"[a-f0-9]{64}", value):
            raise SyncError(f"reviewed_{label}_definition_sha256_invalid")
    return bool(expected_before_sha256)


def apply_live(
    client: N8NClient,
    *,
    repair_existing: bool = False,
    expected_before_sha256: str | None = None,
    expected_after_sha256: str | None = None,
    repair_query_identity: bool = False,
) -> dict[str, Any]:
    reviewed_transition = _reviewed_transition(
        expected_before_sha256,
        expected_after_sha256,
    )
    if reviewed_transition and not repair_existing:
        raise SyncError("reviewed_transition_requires_repair_existing")
    if repair_query_identity and not reviewed_transition:
        raise SyncError("query_identity_repair_requires_exact_transition")

    # In reviewed mode the provider preimage is the first mutable-system
    # prerequisite. No secret/material ensure or credential creator may run
    # before a stale predecessor is rejected.
    existing: list[dict[str, Any]] | None = None
    live: dict[str, Any] | None = None
    before_sha256: str | None = None
    if reviewed_transition:
        existing = [
            item for item in client.list_workflows()
            if item.get("name") == WORKFLOW_NAME
        ]
        if len(existing) > 1:
            raise SyncError("duplicate_receiver_workflows")
        if not existing:
            raise SyncError("reviewed_receiver_workflow_missing")
        live = client.get_workflow(str(existing[0]["id"]))
        before_sha256 = canonical_sha256(workflow_payload(live))
        if before_sha256 != expected_before_sha256:
            raise SyncError("reviewed_before_definition_sha256_mismatch")
        if live.get("active") is not True:
            raise SyncError("reviewed_receiver_workflow_not_active")
        shared_secret = read_existing_secret(SECRET_PATH)
        secret_created = False
    else:
        shared_secret, secret_created = ensure_secret()

    credential_created = False
    if repair_query_identity:
        # Reuse references in the authenticated exact live preimage; no material
        # provisioning, credential listing or generic graph reconstruction.
        candidate = build_query_identity_repair(
            live, hashlib.sha256(shared_secret.encode("utf-8")).hexdigest()
        )
        credential = next(n for n in live["nodes"] if n["name"] == "Authenticated Postback")["credentials"]["httpHeaderAuth"]
    else:
        credentials = [item for item in client.list_credentials() if item.get("name") == CREDENTIAL_NAME]
        if len(credentials) > 1:
            raise SyncError("duplicate_header_credentials")
        if credentials:
            credential = credentials[0]
        else:
            if reviewed_transition:
                raise SyncError("reviewed_header_credential_missing")
            credential = client.create_credential(shared_secret)
            credential_created = True
        candidate = build_workflow(str(credential.get("id") or ""), hashlib.sha256(shared_secret.encode("utf-8")).hexdigest())
    credential_id = str(credential.get("id") or "")
    if not credential_id or credential.get("name") != CREDENTIAL_NAME:
        raise SyncError("header_credential_create_readback_invalid")

    candidate_sha256 = canonical_sha256(candidate)
    if expected_after_sha256 and candidate_sha256 != expected_after_sha256:
        raise SyncError("reviewed_after_definition_sha256_mismatch")
    if existing is None:
        existing = [
            item for item in client.list_workflows()
            if item.get("name") == WORKFLOW_NAME
        ]
    if len(existing) > 1:
        raise SyncError("duplicate_receiver_workflows")
    workflow_created = False
    workflow_updated = False
    if existing:
        if live is None:
            live = client.get_workflow(str(existing[0]["id"]))
            before_sha256 = canonical_sha256(workflow_payload(live))
        if before_sha256 != candidate_sha256:
            if not repair_existing:
                raise SyncError("existing_receiver_workflow_definition_drift")
            if reviewed_transition:
                # n8n has no conditional PUT. Re-read immediately before the
                # single reviewed mutation and fail closed if the preimage moved.
                pre_write = client.get_workflow(str(live["id"]))
                pre_write_sha256 = canonical_sha256(workflow_payload(pre_write))
                if pre_write_sha256 != before_sha256:
                    raise SyncError("reviewed_before_definition_changed_before_write")
                if pre_write_sha256 != expected_before_sha256:
                    raise SyncError("reviewed_before_definition_sha256_mismatch")
            try:
                client.update_workflow(str(live["id"]), candidate)
            except Exception as write_error:
                raise SyncError(
                    "receiver_workflow_write_uncertain_reconcile_without_refire:"
                    + str(write_error)
                ) from write_error
            try:
                repaired = client.get_workflow(str(live["id"]))
                validate_live(repaired, candidate, shared_secret)
            except Exception as verification_error:
                raise SyncError(
                    "receiver_workflow_post_write_reconcile_without_refire:"
                    + str(verification_error)
                ) from verification_error
            workflow_updated = True
            live = repaired
        if live.get("active") is not True:
            if reviewed_transition:
                raise SyncError("reviewed_receiver_workflow_not_active")
            client.activate_workflow(str(live["id"]))
    else:
        created = client.create_workflow(candidate)
        workflow_id = str(created.get("id") or "")
        if not workflow_id:
            raise SyncError("receiver_workflow_create_readback_invalid")
        workflow_created = True
        client.activate_workflow(workflow_id)
        live = client.get_workflow(workflow_id)
    if live is None:
        raise SyncError("receiver_workflow_preimage_not_loaded")
    if not workflow_updated:
        live = client.get_workflow(str(live["id"]))
    verified = validate_live(live, candidate, shared_secret)
    return {
        "schema": "glp-affiliate-postback-n8n-sync-v1",
        "status": "PASS",
        "mcp_quota_fallback": "native_rest_after_mcp_cap_proven",
        "secret_created": secret_created,
        "secret_path": str(SECRET_PATH),
        "secret_permissions": "0600",
        "credential_created": credential_created,
        "credential_id": credential_id,
        "credential_name": CREDENTIAL_NAME,
        "workflow_created": workflow_created,
        "workflow_updated": workflow_updated,
        "write_performed": bool(workflow_created or workflow_updated),
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "before_definition_sha256": before_sha256,
        "after_definition_sha256": candidate_sha256,
        "expected_before_definition_sha256": expected_before_sha256,
        "expected_after_definition_sha256": expected_after_sha256,
        "exact_transition_enforced": reviewed_transition,
        "live": verified,
        "secrets_printed": False,
    }


def inspect(client: N8NClient) -> dict[str, Any]:
    credentials = [item for item in client.list_credentials() if item.get("name") == CREDENTIAL_NAME]
    workflows = [item for item in client.list_workflows() if item.get("name") == WORKFLOW_NAME]
    return {
        "schema": "glp-affiliate-postback-n8n-inspect-v1",
        "status": "READY_TO_CREATE" if not credentials and not workflows else "EXISTING_STATE",
        "credential_count": len(credentials),
        "workflow_count": len(workflows),
        "secret_file_exists": SECRET_PATH.exists(),
        "candidate_sha256": canonical_sha256(build_workflow("CREDENTIAL_ID_PLACEHOLDER")),
        "webhook_url": WEBHOOK_URL,
        "query_webhook_url": QUERY_WEBHOOK_URL,
        "secrets_printed": False,
    }


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    raw = json.dumps(value, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-existing", action="store_true")
    parser.add_argument("--repair-query-identity", action="store_true", help="Preserve the current receiver; extend only account/TEST query fields under exact transition hashes")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--expected-before-sha256")
    parser.add_argument("--expected-after-sha256")
    args = parser.parse_args(argv)
    if args.apply:
        reviewed = _reviewed_transition(
            args.expected_before_sha256,
            args.expected_after_sha256,
        )
        if not args.repair_existing or not reviewed or args.receipt is None:
            raise SyncError("direct_receiver_workflow_write_authority_required")
    client = N8NClient()
    if args.apply:
        result = apply_live(
            client,
            repair_existing=True,
            expected_before_sha256=args.expected_before_sha256,
            expected_after_sha256=args.expected_after_sha256,
            repair_query_identity=args.repair_query_identity,
        )
        try:
            result = authority.attest_receipt(result)
        except authority.AuthorityError as exc:
            raise SyncError(str(exc)) from exc
        write_private_json(args.receipt, result)
        print(json.dumps({
            "status": result["status"],
            "write_performed": result["write_performed"],
            "exact_transition_enforced": result["exact_transition_enforced"],
            "after_definition_sha256": result["after_definition_sha256"],
            "receipt": str(args.receipt),
            "secrets_printed": False,
        }, sort_keys=True))
        return 0
    if args.repair_query_identity:
        live = client.get_workflow("dG2y4DqpoHEMCLfD")
        secret = read_existing_secret(SECRET_PATH)
        candidate = build_query_identity_repair(live, hashlib.sha256(secret.encode()).hexdigest())
        result = {"status":"PREVIEW_QUERY_IDENTITY_REPAIR", "write_performed":False,
            "expected_before_definition_sha256":canonical_sha256(workflow_payload(live)),
            "expected_after_definition_sha256":canonical_sha256(candidate)}
    else:
        result = inspect(client)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
