"""GLP-339 maintained contract check; CI structure != real business acceptance.

The existing scripts/run_tests.sh registers this test. Its direct CLI additionally
reads real retained receipt bytes; it never invokes a pipeline or external tool.
"""
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "ops/pipeline-tool-contracts.json"
PIPELINES = [f"F{i:02d}" for i in range(1, 14)]
NAMES = ["Affiliate supply", "Canonical inbox", "Sheet writer", "Provider data",
         "SEO + community research", "Publishing + deploy", "Analytics + revenue",
         "Ads (parked)", "Decisions + job health", "Host safety",
         "Backup + housekeeping", "Conversions", "Sponsor sales"]
REQUIRED = {
    "wired": ("consumer", "job", "readback_artifact", "evidence",
              "authenticated_scope", "source_window", "operation", "limitations"),
    "hold": ("owner", "reason", "existing_issue", "required_readback"),
    "gap": ("owner", "reason", "existing_issue", "required_readback"),
    "not_applicable": ("reason",),
}
KNOWN_TOOLS = {"posthog", "cogny", "ga4", "gsc", "bing_webmaster", "n8n_mcp",
               "n8n_api", "awin_api", "impact_api", "cj_api", "partnerize_api",
               "affiliati_api", "katalys_api", "everflow_api", "maxbounty_portal",
               "shareasale_legacy", "firecrawl", "google_sheets", "discord", "linear",
               "cloudflare_d1", "google_ads", "meta_ads", "microsoft_ads", "fella_health",
               "gmail", "resend", "suppression_store", "provider_registry", "host_scheduler",
               "github", "partnerstack_api", "admitad_api", "clickbank_api", "refersion_api",
               "healthtrader_api", "clickdealer_api", "digistore24_api", "rook",
               "cloudflare_pages", "host_filesystem", "stripe", "gtm", "indexnow",
               "pro_relay", "claude_cloud", "subscription_telemetry"}


def _contract():
    return json.loads(CONTRACT.read_text())


def _relative_file(root, relative):
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError("unsafe relative path")
    result = (root / p).resolve(strict=True)
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError("path escapes root or is not a file")
    return result


def _pointer(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("nonliteral JSON pointer")
    for key in pointer.split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def validate(contract, repo=REPO, readback_root=None):
    """Return only check results, not fabricated provider/scheduler results."""
    problems = []
    if contract.get("schema") != "glp-pipeline-tool-contracts-v1":
        problems.append(("schema",))
    universe = set(contract.get("tool_universe", {}))
    if not KNOWN_TOOLS <= universe or "additional_reporting_apis" in universe:
        problems.append(("known_tool_denominator", sorted(KNOWN_TOOLS - universe)))
    rows = contract.get("pipelines", {})
    if list(rows) != PIPELINES:
        problems.append(("canonical_13_row_denominator",))
    excluded = contract.get("permanent_exclusions", [])
    if "fella_health" not in excluded:
        problems.append(("permanent_exclusions",))
    verified = []
    for pid, name in zip(PIPELINES, NAMES):
        row = rows.get(pid, {})
        if row.get("definition_status") != "defined" or row.get("name") != name:
            problems.append((pid, "canonical_definition"))
        tools = row.get("tools", {})
        if set(tools) != universe:
            problems.append((pid, "tool_set", sorted(universe ^ set(tools))))
        for tool, entry in tools.items():
            status = entry.get("status")
            if status not in REQUIRED:
                problems.append((pid, tool, "invalid_status", status))
                continue
            for field in REQUIRED[status]:
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    problems.append((pid, tool, "missing", field))
            if status in ("gap", "hold") and not re.fullmatch(r"GLP-\d+", entry.get("existing_issue", "")):
                problems.append((pid, tool, "existing_issue"))
            if tool in excluded and status != "not_applicable":
                problems.append((pid, tool, "excluded_provider_not_excluded"))
            if status != "wired":
                continue
            try:
                _relative_file(repo, entry.get("consumer", ""))
            except (ValueError, OSError):
                problems.append((pid, tool, "consumer_missing_or_unsafe"))
            readback = entry.get("readback", {})
            if not isinstance(readback, dict):
                problems.append((pid, tool, "invalid_readback"))
                continue
            digest = readback.get("sha256", "")
            observed = readback.get("observed")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(observed, dict) or not observed:
                problems.append((pid, tool, "missing_real_receipt_binding"))
                continue
            if readback_root is None:
                continue  # CI explicitly does not claim host receipt verification.
            try:
                receipt = _relative_file(readback_root, entry["readback_artifact"])
                raw = receipt.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("receipt_hash_mismatch")
                actual = json.loads(raw)
                for pointer, expected in observed.items():
                    if _pointer(actual, pointer) != expected:
                        raise ValueError("consumer_observation_mismatch")
                verified.append(f"{pid}/{tool}")
            except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
                problems.append((pid, tool, "real_readback_refused", str(exc)))
    counts = Counter(e.get("status") for row in rows.values() for e in row.get("tools", {}).values())
    return {"status": "PASS" if not problems else "FAIL", "pipeline_count": len(rows),
            "tool_denominator": len(universe), "entry_count": sum(counts.values()),
            "status_counts": dict(counts), "real_readbacks_verified": verified,
            "verification_mode": "retained_receipts" if readback_root is not None else "structure_only",
            "fresh_provider_calls": 0, "business_success_claimed": False, "problems": problems}


def test_pipeline_tool_contracts_are_complete_and_wired_consumers_exist():
    result = validate(_contract())
    assert result["problems"] == []
    assert result["verification_mode"] == "structure_only"
    assert result["real_readbacks_verified"] == []


def test_missing_denominator_unknown_definition_and_source_only_wiring_rejected():
    for mutation in ("missing_row", "missing_tool", "unknown", "source_only", "excluded", "unowned"):
        d = copy.deepcopy(_contract())
        if mutation == "missing_row":
            del d["pipelines"]["F12"]
        elif mutation == "missing_tool":
            del d["pipelines"]["F05"]["tools"]["gsc"]
        elif mutation == "unknown":
            d["pipelines"]["F13"]["name"] = "GLP73 growth retention"
        elif mutation == "source_only":
            del d["pipelines"]["F05"]["tools"]["gsc"]["readback"]
        elif mutation == "excluded":
            d["pipelines"]["F01"]["tools"]["fella_health"]["status"] = "wired"
        else:
            del d["pipelines"]["F12"]["tools"]["cloudflare_d1"]["existing_issue"]
        assert validate(d)["status"] == "FAIL", mutation


def test_receipt_validation_checks_real_bytes_and_literal_consumer_result(tmp_path):
    # Disposable regression fixture proves rejection behavior, never live coverage.
    d = copy.deepcopy(_contract())
    entries = [e for r in d["pipelines"].values() for e in r["tools"].values() if e["status"] == "wired"]
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"consumer": {"state": "fixture-only"}}')
    for e in entries:
        e["readback_artifact"] = "receipt.json"
        e["readback"] = {"sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                         "observed": {"/consumer/state": "fixture-only"}}
    assert validate(d, readback_root=tmp_path)["status"] == "PASS"
    entries[0]["readback"]["observed"]["/consumer/state"] = "fabricated"
    assert validate(d, readback_root=tmp_path)["status"] == "FAIL"
    entries[0]["readback"]["observed"]["/consumer/state"] = "fixture-only"
    receipt.write_text('{"consumer": {"state": "changed"}}')
    assert validate(d, readback_root=tmp_path)["status"] == "FAIL"
    entries[0]["readback_artifact"] = "../receipt.json"
    assert validate(d, readback_root=tmp_path)["status"] == "FAIL"


def test_contract_check_remains_registered_without_a_new_control_plane():
    assert "tests/test_pipeline_tool_contracts.py" in (REPO / "scripts/run_tests.sh").read_text()
    assert _contract()["check"]["registered_in"] == "scripts/run_tests.sh"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readback-root", type=Path, required=True,
                        help="existing retained host receipt root; no network/job effects")
    args = parser.parse_args()
    result = validate(_contract(), readback_root=args.readback_root)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
