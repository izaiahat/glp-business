#!/usr/bin/env python3
"""Registered Price Index recovery; original constructor remains immutable."""
from __future__ import annotations
import argparse
import fcntl
import json
import re
import sys
from pathlib import Path
from typing import Any
sys.path.insert(0, "/home/ubuntu/business/scripts")
from glp_price_index_weekly_successor import (
    GROWTH_ROOT, RELEASES, CURRENT_BASE, ROUTE, PROJECT, RUNTIME_D1, BUSINESS,
    REMINT, LOCK_PATH, _valid_sha256, sha_bytes, iso_now, validate_pointer_shape,
    _validate_base_evidence, load_accepted_failure_contract, credential,
    project_detail, deployment_detail, production_head, d1_ids, readback,
    write_json_0600, run_command, submit_indexnow,
)

def recover_deployed(receipt_path: Path, expected_sha256: str) -> tuple[int, dict[str, Any], Path]:
    """Recover one pinned postdeploy receipt; never construct or deploy a site.

    Invoked only by the registered entry with an explicit parent-pinned receipt.
    Keep the failed run immutable. A durable recovery checkpoint prevents replay
    of a possibly delivered IndexNow request or an unobserved remint.
    """
    if not _valid_sha256(expected_sha256):
        raise RuntimeError("recovery_receipt_sha256_invalid")
    source = receipt_path.resolve(strict=True)
    runs = (GROWTH_ROOT / "runs").resolve(strict=True)
    if receipt_path.is_symlink() or source != receipt_path or source.parent.parent != runs or source.name != "chain-receipt.json":
        raise RuntimeError("recovery_receipt_scope_invalid")
    raw = source.read_bytes()
    if sha_bytes(raw) != expected_sha256:
        raise RuntimeError("recovery_receipt_drift")
    original = json.loads(raw)
    effects = original.get("external_effects", {})
    if (original.get("schema") != "glp-price-index-release-chain-v1"
            or original.get("status") not in {"FAIL", "DEPLOYED_REMINT_PENDING"}
            or effects.get("cloudflare_deployments") != 1
            or any(effects.get(k) != 0 for k in ("current_content_base_remints", "indexnow_pings", "affiliate_mutations", "outreach_sends", "spend_usd"))):
        raise RuntimeError("recovery_effect_state_invalid")
    run_dir = source.parent / "registered-recovery"
    output = run_dir / "chain-receipt.json"
    if output.exists():
        saved = json.loads(output.read_text())
        if saved.get("recovery_source") != {"path": str(source), "sha256": expected_sha256}:
            raise RuntimeError("recovery_checkpoint_identity_drift")
        # Even a prior PASS is a receipt, not authority to replay its effects.
        return (0 if saved.get("status") == "PASS" else 1), saved, output
    receipt = {
        "schema": "glp-price-index-release-chain-v1", "status": "RECOVERY_PREFLIGHT",
        "run_id": original["run_id"] + "-recovery", "started_at_utc": iso_now(),
        "route": ROUTE, "recovery_source": {"path": str(source), "sha256": expected_sha256},
        "deployment": original["deployment"], "release": original["release"],
        "predecessor": original["predecessor"], "candidate": {"observed_on": original.get("candidate", {}).get("observed_on")},
        "commands": [], "external_effects": {k: 0 for k in effects},
        "inherited_effects": effects,
    }
    try:
        release = Path(original["release"]["path"])
        if release.parent != RELEASES or release.resolve(strict=True) != release:
            raise RuntimeError("recovery_release_scope_invalid")
        predecessor = original["predecessor"]
        pointer = json.loads(CURRENT_BASE.read_text())
        validate_pointer_shape(pointer, require_release_dirs=True)
        if any(pointer.get(k) != predecessor.get(k) for k in ("deployment_id", "release_root", "release_tree_sha256", "public_tree_sha256")):
            raise RuntimeError("recovery_predecessor_moved")
        _validate_base_evidence(pointer, Path(pointer["release_root"]))
        acceptance = load_accepted_failure_contract()
        manifest_spec = acceptance.read_bound(original["release"]["publication_allowance"])["release_manifest"]
        acceptance.load_binding({"release_root": str(release), "release_tree_sha256": original["release"]["release_tree_sha256"], "manifest": manifest_spec})
        token = credential("CLOUDFLARE_API_TOKEN", Path("/home/ubuntu/.config/cloudflare/api_token"))
        deployment_id = original["deployment"]["id"]
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", deployment_id):
            raise RuntimeError("recovery_deployment_id_invalid")
        project = project_detail(token)
        detail = deployment_detail(token, deployment_id)
        if (project.get("name") != PROJECT
                or project.get("canonical_deployment", {}).get("id") != deployment_id
                or production_head(token).get("id") != deployment_id
                or detail.get("id") != deployment_id
                or detail.get("environment") != "production"
                or detail.get("latest_stage", {}).get("status") != "success"
                or d1_ids(project) != [RUNTIME_D1] or d1_ids(detail) != [RUNTIME_D1]):
            raise RuntimeError("recovery_authenticated_deployment_mismatch")
        receipt["authenticated_deployment"] = {"id": deployment_id, "url": detail.get("url"), "d1_database_ids": d1_ids(detail)}
        # Compare the immutable and canonical HTML/JSON/CSV before pointer writes.
        receipt["live_readback"] = readback(release, detail)
        receipt["status"] = "RECOVERY_REMINT_ATTEMPTED"
        write_json_0600(output, receipt)
        remint = run_command(receipt, [
            "sudo", "-n", "python3", str(REMINT), "--release", str(release),
            "--deployment-id", deployment_id, "--supersedes-deployment-id", pointer["deployment_id"],
            "--supersedes-release", pointer["release_root"],
        ], cwd=BUSINESS, timeout=600)
        pointer_after = json.loads(CURRENT_BASE.read_text())
        if (pointer_after.get("deployment_id") != deployment_id
                or pointer_after.get("release_root") != str(release)
                or any(pointer_after.get(k) != original["release"].get(k) for k in ("release_tree_sha256", "public_tree_sha256"))):
            raise RuntimeError("content_base_remint_readback_invalid")
        receipt["external_effects"]["current_content_base_remints"] = 1
        receipt["remint"] = {"status": json.loads(remint.stdout).get("status"), "readback": pointer_after}
        receipt["status"] = "REMINTED_SENSOR_PENDING"
        write_json_0600(output, receipt)
        import glp_deploy_sensor_hook as sensor_hook
        sensor_hook.MACHINE_DIR = run_dir
        sensor = sensor_hook.attach_sensor_result(output, release, offline=False, post_alert=False)
        receipt = json.loads(output.read_text())
        receipt["sensor"] = sensor
        if sensor.get("status") not in {"PASS", "PASS_PARTIAL"} or sensor.get("failure_count") != 0:
            raise RuntimeError(f"post_deploy_sensor_failed:{sensor.get('status')}:{sensor.get('failure_count')}")
        receipt["status"] = "INDEXNOW_ATTEMPTED"
        receipt["external_effects"]["indexnow_pings"] = 1
        write_json_0600(output, receipt)
        receipt["indexnow"] = submit_indexnow(release, Path(pointer["release_root"]))
        receipt["status"] = "PASS"
        receipt["blockers"] = []
        code = 0
    except Exception as exc:
        receipt["status"] = "FAIL"
        receipt["blockers"] = [f"{type(exc).__name__}: {exc}"]
        code = 1
    receipt["completed_at_utc"] = iso_now()
    write_json_0600(output, receipt)
    return code, receipt, output



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-receipt", type=Path, required=True)
    parser.add_argument("--resume-receipt-sha256", required=True)
    args = parser.parse_args()
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, receipt, path = recover_deployed(args.resume_receipt, args.resume_receipt_sha256)
    print(json.dumps({
        "status": receipt.get("status"), "receipt": str(path),
        "observed_on": receipt.get("candidate", {}).get("observed_on"),
        "deployment_id": receipt.get("deployment", {}).get("id"),
        "sensor_status": receipt.get("sensor", {}).get("status"),
        "indexnow_status": receipt.get("indexnow", {}).get("status"),
        "blockers": receipt.get("blockers", []),
        "external_effects": receipt.get("external_effects", {}),
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
