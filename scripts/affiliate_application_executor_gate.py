#!/usr/bin/env python3
"""Fail-closed preflight for future GLP affiliate provider-mutating adapters.

This script never calls a provider. A provider adapter may execute only from a PASS
receipt produced for the exact target manifest and authority manifest bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    from affiliate_niche_geo_filter import is_https_application_url
except ImportError:
    from scripts.affiliate_niche_geo_filter import is_https_application_url

EXPECTED_AUTHORITY_SCHEMA = "glp_affiliate_auto_application_authority_v1"
EXPECTED_PREFLIGHT_SCHEMA = "glp_affiliate_execution_preflight_v1"
# The niche classifier's breadth tiers are evidence vocabulary, while the
# signed authority manifest names broader execution lanes.  Admit only these
# exact, reviewed aliases and only when their canonical parent lane remains in
# the active manifest.  Unknown/new tiers therefore stay fail-closed.
#
# EMPTY since the 2026-09-17 apply-scope ruling (receipt ops/linear/approvals/
# OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json):
# the three aliases that lived here — telehealth_clinical -> other,
# compounding_pharmacy -> glp1-weightloss, glp_adjacent_labs -> glp1-weightloss
# — existed only to carry the 2026-08-13 breadth tiers into an authorized lane,
# and those tiers are no longer application fits.  The mechanism stays so a
# future reviewed alias has a declared home; with no aliases, a fit_lane must
# appear verbatim in the ACTIVE manifest lanes or the row fails closed.
FIT_TIER_AUTHORITY_LANES: dict[str, str] = {}
CONSUMED_STATUSES = {
    "SUBMITTED_VERIFIED", "APPROVED_VERIFIED", "JOINED_VERIFIED",
    "ALREADY_RELATIONSHIP", "AUTO_APPROVED_VERIFIED", "PENDING_VERIFIED",
}
REQUIRED_FALSE_FLAGS = {
    "requires_account_creation", "requires_signature", "requires_kyc",
    "requires_tax_action", "requires_banking_or_payment", "requires_security_change",
    "requires_provider_outreach", "has_unresolved_custom_terms",
    "has_unsupported_medical_claims", "creates_phi_role", "brand_sensitive",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fit_lane_authorized(fit_lane: Any, authority_lanes: Any) -> bool:
    """Bind an exact classifier tier to an already-authorized parent lane."""
    lanes = ({lane for lane in authority_lanes if isinstance(lane, str) and lane}
             if isinstance(authority_lanes, list) else set())
    if not isinstance(fit_lane, str) or not fit_lane:
        return False
    if fit_lane in lanes:
        return True
    parent_lane = FIT_TIER_AUTHORITY_LANES.get(fit_lane)
    return parent_lane is not None and parent_lane in lanes


def identity(row: dict[str, Any]) -> str:
    network = str(row.get("network") or "").strip()
    program_id = str(
        row.get("program_id") or row.get("merchant_id") or row.get("advertiser_id")
        or row.get("offer_id") or row.get("campaign_id") or ""
    ).strip()
    if not network or not program_id:
        raise RuntimeError("target_missing_exact_network_or_immutable_program_id")
    return f"{network}:{program_id}"


def collect_consumed(paths: list[Path]) -> set[str]:
    consumed: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            status = str(value.get("status") or value.get("action") or "").upper()
            if status in CONSUMED_STATUSES or str(value.get("relationship") or "").lower() in {"pending", "approved", "joined", "active"}:
                try:
                    consumed.add(identity(value))
                except RuntimeError:
                    pass
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for path in paths:
        walk(json.loads(path.read_text()))
    return consumed


def validate(authority_path: Path, preflight_path: Path, ledger_paths: list[Path]) -> dict[str, Any]:
    authority = json.loads(authority_path.read_text())
    preflight = json.loads(preflight_path.read_text())
    failures: list[str] = []
    if authority.get("schema") != EXPECTED_AUTHORITY_SCHEMA or authority.get("status") != "ACTIVE":
        failures.append("authority_not_active_or_schema_mismatch")
    if authority.get("authority") != "advertiser_program_application_submission_only":
        failures.append("authority_scope_mismatch")
    if preflight.get("schema") != EXPECTED_PREFLIGHT_SCHEMA:
        failures.append("preflight_schema_mismatch")
    authority_sha = digest(authority_path)
    if preflight.get("authority_manifest_sha256") != authority_sha:
        failures.append("authority_manifest_hash_mismatch")
    rows = preflight.get("targets") or []
    if not rows:
        failures.append("no_targets")
    consumed = collect_consumed(ledger_paths)
    seen: set[str] = set()
    approved: list[str] = []
    for row in rows:
        try:
            target = identity(row)
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        if target in seen:
            failures.append(f"duplicate_target:{target}")
        seen.add(target)
        if not is_https_application_url(row.get("application_url") or row.get("apply_url") or row.get("url")):
            failures.append(f"missing_application_url:{target}")
        if target in consumed:
            failures.append(f"consumed_or_existing_relationship:{target}")
        if str(row.get("decision") or "").upper() != "AUTO_APPLY":
            failures.append(f"decision_not_auto_apply:{target}")
        if not fit_lane_authorized(row.get("fit_lane"), authority.get("lanes")):
            failures.append(f"fit_lane_not_authorized:{target}")
        if str(row.get("relationship") or "").lower() not in {"available", "notjoined", "not_joined"}:
            failures.append(f"relationship_not_fresh_available:{target}")
        for key in ("first_party_evidence_path", "terms_evidence_path", "payload_path"):
            evidence = Path(str(row.get(key) or ""))
            if not evidence.is_absolute():
                evidence = Path("/home/ubuntu/business") / evidence
            expected_sha = str(row.get(key.replace("_path", "_sha256")) or "")
            if not evidence.is_file():
                failures.append(f"missing_{key}:{target}")
            elif not expected_sha or digest(evidence) != expected_sha:
                failures.append(f"hash_mismatch_{key}:{target}")
        terms = row.get("terms") or {}
        if terms.get("captured_complete") is not True or terms.get("compatible") is not True:
            failures.append(f"terms_not_complete_compatible:{target}")
        if terms.get("has_special_terms") is True:
            failures.append(f"special_terms_require_operator_decision:{target}")
        for flag in REQUIRED_FALSE_FLAGS:
            if row.get(flag) is not False:
                failures.append(f"gate_not_explicit_false:{target}:{flag}")
        if not any(item.startswith(("consumed_or_existing_relationship:", "decision_not_auto_apply:", "fit_lane_not_authorized:", "relationship_not_fresh_available:", "missing_", "hash_mismatch_", "terms_not_", "special_terms_", "gate_not_")) and target in item for item in failures):
            approved.append(target)
    return {
        "schema": "glp_affiliate_executor_gate_receipt_v1",
        "status": "PASS" if not failures else "BLOCKED",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "authority_manifest": str(authority_path),
        "authority_manifest_sha256": authority_sha,
        "preflight_manifest": str(preflight_path),
        "preflight_manifest_sha256": digest(preflight_path),
        "approved_exact_identities": approved if not failures else [],
        "failures": sorted(set(failures)),
        "effects": {"provider_calls": 0, "provider_writes": 0, "sheet_calls": 0, "sheet_writes": 0},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authority", required=True)
    parser.add_argument("--preflight", required=True)
    parser.add_argument("--consumption-ledger", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    receipt = validate(
        Path(args.authority),
        Path(args.preflight),
        [Path(path) for path in args.consumption_ledger],
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(output, 0o600)
    print(json.dumps({
        "status": receipt["status"],
        "approved_exact_identities": receipt["approved_exact_identities"],
        "failure_count": len(receipt["failures"]),
        "output": str(output),
    }, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
