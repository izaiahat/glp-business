#!/usr/bin/env python3
"""Build read-only marketplace coverage evidence for the Monday discovery card."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

BUSINESS = Path(__file__).resolve().parents[1]
MARKETPLACE_WORKFLOW_ID = "iOJhvTtehlHGLUSs"
WORKFLOW_MARKETPLACES = {
    "awin", "cj", "rakuten", "partnerize", "digistore24", "partnerstack",
    "refersion", "healthtrader", "maxbounty",
}
NON_JOINED_OR_MERGED = {
    "flexoffers": "publisher application declined; not a joined marketplace",
    "shareasale": "merged into Awin; scanned through Awin",
}


def build(local_summary: dict, execution: dict, *, joined=None) -> dict:
    sys.path.insert(0, str(BUSINESS / "scripts"))
    import affiliate_supply_discovery as discovery  # noqa: E402
    registry = set(discovery.memberships() if joined is None else joined)
    effective = registry - set(NON_JOINED_OR_MERGED)
    local = set((local_summary.get("networks") or {}).keys())
    local.update((local_summary.get("portal_only") or {}).keys())
    # A successful workflow execution doesn't prove each catalog completed.
    n8n = set(execution.get("completed_networks") or [])
    attempted = local | n8n
    completed = {network for network, receipt in (local_summary.get("networks") or {}).items()
                 if isinstance(receipt, dict) and receipt.get("complete") is True}
    covered = sorted(effective & (completed | n8n))
    missing = sorted(effective - set(covered))
    return {
        "schema": "affiliate-monday-marketplace-coverage-v1",
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "LOCAL_EVIDENCE_ONLY",
        "scope": "historical local receipts; not a new authenticated scan",
        "workflow_id": MARKETPLACE_WORKFLOW_ID,
        "execution_id": str(execution.get("id") or ""),
        "execution_status": str(execution.get("status") or "missing"),
        "execution_started_at": str(execution.get("startedAt") or ""),
        "local_sweep_captured_at": str(local_summary.get("captured_at_utc") or ""),
        "coverage": {"expected": len(effective), "completed": len(covered),
                     "attempted": sorted(effective & attempted),
                     "joined_marketplaces": sorted(effective), "covered": covered,
                     "missing": missing},
        "known_dispositions": NON_JOINED_OR_MERGED,
        "local_gaps": local_summary.get("gaps") or [],
        "effects": {"applications_submitted": 0, "provider_mutations": 0,
                    "marketplaces_joined": 0, "sheet_writes": 0},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-summary", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        local = json.loads(Path(args.local_summary).read_text())
    except (OSError, ValueError):
        local = {}
    payload = build(local, {})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(output, 0o600)
    print(json.dumps({"status": payload["status"], "execution_id": payload["execution_id"],
                      "coverage": payload["coverage"], "receipt": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
