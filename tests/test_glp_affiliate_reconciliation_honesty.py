from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path("/home/ubuntu/business/scripts/glp_affiliate_reconciliation_honesty.py")
STALE_RECEIPT = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-reconciliation-20260802/"
    "daily/2026-08-13/32-daily-final-receipt.json"
)
REAL_PASS_PROBE = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/"
    "enumeration-20260814T034425Z/rakuten-inventory.json"
)
MACHINE_DIR = Path("/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine")
BLOCKER_STORE = MACHINE_DIR / "operator-blockers.json"

SPEC = importlib.util.spec_from_file_location("reconciliation_honesty", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules["reconciliation_honesty"] = mod
SPEC.loader.exec_module(mod)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_probes() -> dict[str, dict]:
    return mod.discover_fresh_pass_probes(
        probe_root=MACHINE_DIR,
        now_utc=dt.datetime(2026, 8, 14, 4, 0, tzinfo=dt.timezone.utc),
    )


def test_real_stale_reconciliation_event_is_rejected_by_real_fresh_pass_probe() -> None:
    """Provider-free regression over the exact bytes that exposed queue defect 2c."""
    assert _sha(STALE_RECEIPT) == "8cf9e5b47be08a4f16b5a2cd5cea4fe67c46e2c18925f799b6bf3799a6ae7671"
    assert _sha(REAL_PASS_PROBE) == "8a5e096d2c0ff08b89c9be343c02a3dc20b7efb2d241bcd65b7895a4fd5a6ac5"
    probes = _real_probes()
    assert probes["rakuten"]["auth_state"] == "PASS"
    assert probes["rakuten"]["blockers_count"] == 0

    receipt = json.loads(STALE_RECEIPT.read_text(encoding="utf-8"))
    card = (
        "Rakuten — existing Publisher Dashboard; securely hand off the SID plus "
        "Developer Portal client ID/secret or token-key, then reply RAKUTEN OAUTH READY."
    )
    violations = mod.validate_payload(receipt, card, probes)
    assert any(row["network"] == "rakuten" for row in violations)
    assert any(row["code"] == "fresh_sibling_pass_forbids_operator_owed_blocker" for row in violations)
    assert any(row["code"] == "fresh_sibling_pass_forbids_stale_auth_blocker" for row in violations)


def test_corrected_real_event_shape_passes_and_current_blocker_store_has_no_conflict() -> None:
    probes = _real_probes()
    receipt = copy.deepcopy(json.loads(STALE_RECEIPT.read_text(encoding="utf-8")))
    receipt["reason"] = "All local deterministic reconciliation checks passed; provider and release gates remain."
    receipt["marketplaces"]["counts"]["Rakuten"] = {
        "partnered": 0,
        "catalog_auth": "PASS",
        "catalog_enumerated": probes["rakuten"]["enumerated"],
    }
    receipt["blockers"] = [
        row for row in receipt["blockers"] if "rakuten" not in row.lower()
    ]
    card = "Rakuten auth: PASS via current enumerator · operator action: none"
    assert mod.blocker_conflicts(probes, BLOCKER_STORE) == []
    assert mod.validate_payload(receipt, card, probes) == []


def test_pass_probe_older_than_24_hours_does_not_suppress_a_real_blocker(tmp_path: Path) -> None:
    enumeration = tmp_path / "enumeration-old"
    enumeration.mkdir()
    probe = json.loads(REAL_PASS_PROBE.read_text(encoding="utf-8"))
    probe["captured_at_utc"] = "2026-08-12T03:44:45Z"
    (enumeration / "rakuten-inventory.json").write_text(json.dumps(probe), encoding="utf-8")
    probes = mod.discover_fresh_pass_probes(
        probe_root=tmp_path,
        now_utc=dt.datetime(2026, 8, 14, 4, 0, tzinfo=dt.timezone.utc),
    )
    assert "rakuten" not in probes
    receipt = json.loads(STALE_RECEIPT.read_text(encoding="utf-8"))
    assert mod.validate_payload(receipt, "", probes) == []
