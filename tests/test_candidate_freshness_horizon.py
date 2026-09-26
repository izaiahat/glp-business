"""Regression for candidate freshness gates defined only in src/."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "candidate_freshness_horizon", ROOT / "scripts/candidate_freshness_horizon.py"
)
HORIZON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HORIZON)


def test_src_only_price_tracker_gate_has_source_and_expiry(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src" / "price-tracker.mjs").write_text(
        "const MAX_AGE_MS = 45 * 86_400_000;\n"
        "if (nowMs - checkedMs > MAX_AGE_MS) throw new Error('tracker_freshness_invalid');\n"
    )
    (tmp_path / "data" / "price-tracker.json").write_text(
        '{"records":[{"id":"fixture","checked_at":"2026-09-01T00:00:00Z"}]}'
    )

    gates = HORIZON.inventory(tmp_path, datetime(2026, 9, 24, tzinfo=timezone.utc))
    price = next(gate for gate in gates if gate["gate"] == "price-tracker")
    assert price["source"] == "src/price-tracker.mjs:2"
    assert price["window_days"] == 45
    assert price["evidence_timestamp"] == "2026-09-01T00:00:00Z"
    assert price["expiry_date"] == "2026-10-16"


def test_unknown_gate_is_reported_and_exits_nonzero(monkeypatch, capsys):
    unknown = {"gate": "price-alert", "source": "src/price-tracker.mjs:207",
               "window_days": 45, "window_hours": None, "evidence_timestamp": "UNKNOWN",
               "expiry_date": "UNKNOWN", "status": "UNKNOWN"}
    monkeypatch.setattr(HORIZON, "inventory", lambda root, now: [unknown])
    assert HORIZON.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "UNKNOWN"
    assert result["unknown"] == [unknown]


def test_pricing_witness_and_legacy_noon_clocks_are_discovered(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src/pricing-evidence-v2.mjs").write_text("evidence_older_than_recency_bar\n")
    (tmp_path / "scripts/validate-provider-pricing-matrix.mjs").write_text(
        "const MAX_EVIDENCE_AGE_DAYS = 92;\n"
        "if (ageInDays(row.captured_on, now) > MAX_EVIDENCE_AGE_DAYS) fail();\n"
    )
    (tmp_path / "data/provider-pricing-matrix.json").write_text(json.dumps({"providers": {
        "one": {"medications": [
            {"row_id": "one:typed", "price_kind": "quoted", "evidence": [
                {"captured_at_utc": "2026-09-08T03:30:55Z"}]},
            {"medication_id": "legacy", "captured_on": "2026-08-31"},
        ]},
    }}))

    gates = {gate["gate"]: gate for gate in HORIZON.inventory(
        tmp_path, datetime(2026, 9, 24, tzinfo=timezone.utc)
    )}
    assert gates["pricing-typed-witness"]["source"] == "src/pricing-evidence-v2.mjs:1"
    assert gates["pricing-typed-witness"]["window_days"] == 92
    assert gates["pricing-typed-witness"]["expiry_date"] == "2026-12-09"
    assert gates["pricing-legacy-capture"]["source"] == "scripts/validate-provider-pricing-matrix.mjs:2"
    assert gates["pricing-legacy-capture"]["evidence_timestamp"] == "2026-08-31T12:00:00Z"
    assert gates["pricing-legacy-capture"]["expiry_date"] == "2026-12-01"


def test_adopted_deal_clock_is_distinct_from_frozen_base(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "artifacts/ops").mkdir(parents=True)
    (tmp_path / "src/deal-ingestion.mjs").write_text(
        "const MAX_EVIDENCE_AGE_DAYS = 45;\n"
        "throw new Error('offer_evidence_stale');\n"
        "evaluated_at: receipt.fetched_at,\n"
    )
    (tmp_path / "data/deals.json").write_text(json.dumps({"offers": [
        {"id": name, "status": "VERIFIED", "checked_at": "2026-08-02T01:41:56Z"}
        for name in ("base", "accepted")
    ]}))
    (tmp_path / "artifacts/ops/wave4-deal-ingestion-manifest.json").write_text(
        '{"evaluated_at":"2026-08-08T23:52:45Z"}'
    )
    monkeypatch.setattr(HORIZON, "qualification_clocks", lambda root: {
        "material_offers": [{"offer_id": "accepted", "fetched_at": "2026-09-14T19:27:04.300Z"}],
        "qualifications": {},
    })
    gates = {gate["gate"]: gate for gate in HORIZON.inventory(
        tmp_path, datetime(2026, 9, 24, tzinfo=timezone.utc)
    )}
    assert gates["deals"]["row"] == "base"
    assert gates["deals"]["evaluation_clock"] == "historical_as_of"
    assert gates["deal-material-offer"]["row"] == "accepted"
    assert gates["deal-material-offer"]["source"] == "src/deal-ingestion.mjs:3"
    assert gates["deal-material-offer"]["evidence_timestamp"] == "2026-09-14T19:27:04.300Z"
    assert gates["deal-material-offer"]["expiry_at_utc"] == "2026-10-29T19:27:04.300000+00:00"


def test_css_missing_required_pair_and_hash_drift_are_not_current(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "assets/css-src").mkdir(parents=True)
    (tmp_path / "public/assets").mkdir(parents=True)
    (tmp_path / "src/css-build-freshness.mjs").write_text(
        'const COMMON = ["00-foundation.css", "01-primitives.css"];\n'
        '"site-home.css": [...COMMON, "10-home.css"],\n'
        'throw new Error(`css_bundle_stale:${bundle}:${layer}`);\n'
        'throw new Error(`public_css_stale:${bundle}`);\n'
    )
    (tmp_path / "assets/css-src/00-foundation.css").write_text("foundation")
    (tmp_path / "assets/site.css").write_text("compiled")
    (tmp_path / "public/assets/site.css").write_text("compiled")
    gates = {gate["gate"]: gate for gate in HORIZON.css_gates(tmp_path)}
    assert gates["css-bundle"]["status"] == "UNKNOWN"
    assert "assets/css-src/01-primitives.css" in gates["css-bundle"]["missing_required_paths"]
    assert "assets/site-home.css" in gates["css-bundle"]["missing_required_paths"]
    assert gates["public-css"]["status"] == "UNKNOWN"
    assert "public/assets/site-home.css" in gates["public-css"]["missing_required_paths"]

    (tmp_path / "assets/css-src/01-primitives.css").write_text("primitives")
    (tmp_path / "assets/css-src/10-home.css").write_text("home")
    (tmp_path / "assets/site-home.css").write_text("compiled home")
    (tmp_path / "public/assets/site-home.css").write_text("compiled home")
    (tmp_path / "public/assets/site.css").write_text("different bytes")
    gates = {gate["gate"]: gate for gate in HORIZON.css_gates(tmp_path)}
    assert gates["public-css"]["status"] == "STALE"
    assert gates["public-css"]["hash_drift"] is True
