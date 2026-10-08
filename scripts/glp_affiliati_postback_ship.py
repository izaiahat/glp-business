#!/usr/bin/env python3
"""Registered Affiliati consumer: reconcile the accepted modern F06 release.

The August overlay was superseded. This consumer cannot publish, migrate,
change bindings or restore old receiver bytes. A different release stays with
its existing carrier and refuses here until independently accepted. The exact
predecessor is retained in Git for supported baseline adoption.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from glp_deploy_build6_authority_v1 import (
    ACCOUNT_ID, PROJECT, CloudflareClient, deployment_commit, production_deployment,
    read_token, successful_production,
)

BUSINESS = Path('/home/ubuntu/business')
NETWORK_SPEC = BUSINESS / 'reports/glp-clinic-scout/affiliate-machine/postback/network-specs/affiliati.json'
CURRENT_BASE = BUSINESS / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
LOCK_PATH = Path('/home/ubuntu/.hermes/locks/glp-price-index-weekly.lock')
RECEIPT_DIR = BUSINESS / 'reports/ops/job-receipts'
ENDPOINT = 'https://glpclinicscout.com/api/postback/affiliati'
D1_ID = 'bcb91d2d-c3f1-4c55-93e3-070587e86d60'
# Pinned receiver: String(env.AFFILIATI_POSTBACK_ACCOUNT_REF || "15309") gates the
# owned-click join, so the runtime override must be proven, not inferred from bytes.
ACCOUNT_REF = '15309'
ACCOUNT_REF_VAR = 'AFFILIATI_POSTBACK_ACCOUNT_REF'
# Exact accepted October 3 F06 cohort, not permission to accept successors.
ACCEPTED = {
    'deployment_id': '8bb93c91-59fe-4a3b-b050-a956479c6ef5',
    'site_head': '2359d8db82563032030d55dc35e2937ab4883357',
    'manifest_sha256': '573c79ae530f39b7908fc7647666c27a07cba7a5d8cff5c836a55aad8b66cad6',
    'terminal_sha256': 'c87e5d927645d3d23a3fdc1cfc86e3297750d5864530b8bd82c409dd29dbe7b9',
    'receiver_sha256': 'a52872f95567bfe226dd126fd633506d75e2fa832644e33f7a9ff21c4356af01',
}


class ShipError(RuntimeError):
    pass


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ShipError('input_not_regular')
    obj = json.loads(path.read_bytes())
    if not isinstance(obj, dict):
        raise ShipError('input_not_object')
    return obj


def binding(path: Path) -> dict[str, str]:
    return {'path': str(path), 'sha256': sha_file(path)}


def validate_predeploy_control_artifacts() -> dict[str, Any]:
    """Consume the maintained spec without changing its producer or output."""
    spec = read_json_object(NETWORK_SPEC)
    if (spec.get('schema') != 'glp.postback_network_spec.v1'
            or spec.get('network') != 'Affiliati' or spec.get('support', {}).get('supported') is not True
            or spec.get('receiver_endpoint') != ENDPOINT or spec.get('callback_method') != 'GET'
            or spec.get('configuration_scope') != 'per_offer'
            or spec.get('offers') != ['40044', '42896', '43194']):
        raise ShipError('network_spec_not_ready')
    expected = {'subid': '#s2#', 'click_id': '#s2#', 'placement': '#s1#',
                'conversion_id': '#leadid#', 'offer_id': '#oid#', 'payout': '#price#', 'event': 'conversion'}
    if spec.get('click_id_macro') != '#s2#' or spec.get('callback_query') != expected:
        raise ShipError('network_spec_identity_contract_invalid')
    auth = spec.get('authentication', {})
    if any(auth.get(k) != v for k, v in {
        'capability': 'supported', 'mode': 'shared_secret', 'placement': 'query', 'parameter': 'key',
        'secret_ref': '/home/ubuntu/.hermes/secrets/affiliati/postback-key.txt',
    }.items()):
        raise ShipError('network_spec_authentication_contract_invalid')
    return {'network_spec': binding(NETWORK_SPEC), 'click_id_macro': '#s2#', 'placement_macro': '#s1#'}


def validate_current_release() -> dict[str, Any]:
    """Bind independent terminal acceptance and the actual receiver bytes."""
    controls = validate_predeploy_control_artifacts()
    pointer = read_json_object(CURRENT_BASE)
    if (pointer.get('schema') != 'glp-opus-current-content-base-v1' or pointer.get('status') != 'PASS'
            or any(pointer.get(k) != ACCEPTED[k] for k in ('deployment_id', 'site_head'))):
        raise ShipError('current_release_not_accepted')
    refs, objects = {}, {}
    for field, digest_key in [('manifest', 'manifest_sha256'), ('final_receipt', 'terminal_sha256')]:
        ref = pointer.get(field, {})
        path = Path(ref.get('path', ''))
        if (not path.is_absolute() or ref.get('sha256') != ACCEPTED[digest_key]
                or path.is_symlink() or sha_file(path) != ref['sha256']):
            raise ShipError('terminal_input_binding_invalid')
        refs[field] = binding(path)
        objects[field] = read_json_object(path)
    manifest, terminal = objects['manifest'], objects['final_receipt']
    release = Path(pointer.get('release_root', ''))
    if (not release.is_absolute() or release.is_symlink() or not release.is_dir()
            or manifest.get('release_root') != str(release)
            or manifest.get('site_head') != pointer['site_head']
            or manifest.get('release_tree_sha256') != pointer.get('release_tree_sha256')
            or terminal.get('schema') != 'glp-opus-production-terminal-receipt-v1'
            or terminal.get('status') != 'PASS_PRODUCTION_TERMINAL'
            or terminal.get('deployment', {}).get('deployment_id') != pointer['deployment_id']
            or terminal.get('site_head') != pointer['site_head']
            or terminal.get('evidence_sha256', {}).get('01-deployment-manifest.json') != refs['manifest']['sha256']
            or terminal.get('immutable_closure', {}).get('release_tree_sha256') != pointer.get('release_tree_sha256')
            or terminal.get('immutable_closure', {}).get('public_tree_sha256') != pointer.get('public_tree_sha256')):
        raise ShipError('terminal_release_chain_invalid')
    for rel in ('functions/api/postback/affiliati.js', 'src/affiliati-attribution.mjs',
                'data/affiliati-postback-contract.json'):
        path = release / rel
        rows = [r for r in manifest.get('release_rows', []) if r.get('path') == rel]
        if path.is_symlink() or not path.is_file() or len(rows) != 1 or sha_file(path) != rows[0]['sha256']:
            raise ShipError('receiver_manifest_binding_invalid')
        refs[rel] = binding(path)
    if refs['functions/api/postback/affiliati.js']['sha256'] != ACCEPTED['receiver_sha256']:
        raise ShipError('accepted_receiver_source_drift')
    contract = read_json_object(release / 'data/affiliati-postback-contract.json')
    if (contract.get('schema') != 'affiliati-receiver-contract-v4-registered-subid-owned-click-join'
            or contract.get('affiliate_id') != '15309' or contract.get('public_endpoint') != ENDPOINT
            or any(contract.get('parameter_map', {}).get(k, {}).get('macro') != v
                   for k, v in {'subid': '#s2#', 'click_id': '#s2#', 'placement': '#s1#'}.items())
            or contract.get('auth', {}).get('secret_binding') != 'AFFILIATI_POSTBACK_KEY'
            or contract.get('persistence', {}).get('database_binding') != 'GLP_DB'
            or contract.get('persistence', {}).get('d1_database_id') != D1_ID):
        raise ShipError('accepted_receiver_contract_invalid')
    # Historical candidate status is not rewritten; terminal acceptance wins.
    return {**controls, 'current_pointer': binding(CURRENT_BASE), 'inputs': refs,
            'deployment_id': pointer['deployment_id'], 'site_head': pointer['site_head']}


def effective_account_ref(env_vars: Any) -> str:
    """Absent uses the pinned default; only verifiable plain text may override.

    Opaque (secret) or malformed metadata stays UNKNOWN and refuses; no secret
    value is ever retrieved to resolve it.
    """
    if not isinstance(env_vars, dict):
        raise ShipError('receiver_account_binding_invalid')
    if ACCOUNT_REF_VAR not in env_vars:
        return ACCOUNT_REF
    var = env_vars[ACCOUNT_REF_VAR]
    if (not isinstance(var, dict) or var.get('type') != 'plain_text'
            or var.get('value') != ACCOUNT_REF):
        raise ShipError('receiver_account_binding_invalid')
    return ACCOUNT_REF


def reconcile(client=None) -> dict[str, Any]:
    local = validate_current_release()
    client = client if client is not None else CloudflareClient(read_token())
    project = client.project()
    canonical = production_deployment(project)
    detail = client.request('GET', f'/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{local["deployment_id"]}')
    for item in (canonical, detail):
        if (not successful_production(item) or item.get('id') != local['deployment_id']
                or deployment_commit(item) != local['site_head']
                or item.get('d1_databases', {}).get('GLP_DB', {}).get('id') != D1_ID
                or item.get('env_vars', {}).get('AFFILIATI_POSTBACK_KEY', {}).get('type') != 'secret_text'):
            raise ShipError('canonical_receiver_readback_invalid')
        effective_account_ref(item.get('env_vars'))
    config = project.get('deployment_configs', {}).get('production', {})
    if (config.get('d1_databases', {}).get('GLP_DB', {}).get('id') != D1_ID
            or config.get('env_vars', {}).get('AFFILIATI_POSTBACK_KEY', {}).get('type') != 'secret_text'):
        raise ShipError('production_receiver_bindings_invalid')
    effective_account_ref(config.get('env_vars'))
    if validate_current_release() != local:
        raise ShipError('release_inputs_changed_during_readback')
    return {**local, 'status': 'SKIP_ALREADY_INSTALLED', 'mode': 'accepted_current_release_reconciliation',
            'production_deployment_id': local['deployment_id'],
            'canonical_readback': {'id': canonical['id'], 'site_head': deployment_commit(canonical),
                                   'D1_id': D1_ID, 'secret_binding_type': 'secret_text',
                                   'account_ref': ACCOUNT_REF},
            'effects': {'deployments': 0, 'D1_writes': 0, 'Sheet_writes': 0, 'provider_mutations': 0,
                        'tracking_requests': 0, 'receiver_changes': 0},
            'proof_limit': 'Accepted F06 terminal reconciliation only; not a new deployment, conversion, commission or cash.'}


def run() -> tuple[int, dict[str, Any]]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75, {'status': 'DEFERRED_RELEASE_LOCK_BUSY', 'reason': 'release_lock_busy'}
        receipt = {'schema': 'glp-affiliati-postback-ship-receipt-v2',
                   'started_at_utc': datetime.now(timezone.utc).isoformat()}
        try:
            receipt.update(reconcile())
            rc = 0
        except Exception as exc:
            # Never serialize provider responses, secrets or input contents.
            receipt.update(status='REFUSED', reason=str(exc) if isinstance(exc, ShipError) else type(exc).__name__)
            rc = 1
        receipt['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = RECEIPT_DIR / f'affiliati-postback-ship-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}.json'
        receipt['receipt_path'] = str(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        return rc, receipt


def main() -> int:
    rc, receipt = run()
    print(json.dumps({'status': receipt['status'], 'reason': receipt.get('reason'),
                      'receipt': receipt.get('receipt_path'),
                      'deployment_id': receipt.get('deployment_id'),
                      'effects': receipt.get('effects', {})}, sort_keys=True))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
