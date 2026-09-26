"""Regression for candidate freshness gates defined only in src/."""
from datetime import datetime, timezone
import importlib.util
import json
import hashlib
import os
import pytest
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
    monkeypatch.setattr(HORIZON, "source_coverage", lambda root: [])
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


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)

# Synthetic contracts only: no provider data or external authority is curated.
KINDS = [
    ("deals", "deal-ingestion.mjs", "const MAX_EVIDENCE_AGE_DAYS = 45;\noffer_evidence_stale",
     "deals.json", {"offers": [{"id": "one", "status": "VERIFIED", "checked_at": "2026-09-16"}]}, 45, "2026-10-31"),
    ("original-study", "original-data.mjs", 'throw new Error("study_source_stale")',
     "original-data-study.json", {"as_of_utc": "2026-09-16", "methodology": {"freshness_max_age_hours": 72},
                                  "sources": [{"source_id": "one", "checked_at": "2026-09-16"}]}, 72, "2026-09-19"),
    ("mystery-observation", "mystery-shop.mjs", 'const MAX_AGE_MS = 45 * DAY_MS;\nparseFreshInstant(qualification ? qualification.fetched_at : row.observed_at',
     "mystery-shop.json", {"observations": [{"id": "one", "observed_at": "2026-09-16"}]}, 45, "2026-10-31"),
    ("promo-code", "promo-code.mjs", "export const PROMO_MAX_AGE_DAYS = 45;\npromo_evidence_stale",
     "money-page-specs.json", {"pages": [{"program_id": "one", "promo": {"status": "VERIFIED", "verified_as_of": "2026-09-16"}}]}, 45, "2026-10-31"),
    ("dynamic-profile", "provider-dynamic-profiles.mjs", 'fail("evidence_stale", id)',
     "program-registry.json", {"provider_dynamic_profiles": {"freshness_max_days": 90, "profiles": {"one": {"verified_as_of": "2026-09-16"}}}}, 90, "2026-12-15"),
    ("therapeutic-taxonomy", "therapeutic-taxonomy.mjs", 'fail("evidence_stale", id)',
     "program-registry.json", {"therapeutic_taxonomy": {"freshness_max_days": 31, "programs": {"one": {"verified_as_of": "2026-09-16"}}}}, 31, "2026-10-17"),
    ("price-tracker", "price-tracker.mjs", 'const MAX_AGE_MS = 45 * 86_400_000;\ntracker_freshness_invalid',
     "price-tracker.json", {"records": [{"id": "one", "checked_at": "2026-09-16"}]}, 45, "2026-10-31"),
]


@pytest.mark.parametrize("kind,src,code,file,data,window,expiry", KINDS, ids=[row[0] for row in KINDS])
def test_each_calendar_clock(kind, src, code, file, data, window, expiry, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src" / src).write_text(code)
    (tmp_path / "data" / file).write_text(json.dumps(data))
    row = next(r for r in HORIZON.inventory(tmp_path, NOW) if r["gate"] == kind)
    assert row["source"] == f"src/{src}:{len(code.splitlines())}"
    assert (row["window_hours"] if kind == "original-study" else row["window_days"]) == window
    assert row["evidence_timestamp"] == "2026-09-16"
    assert row["expiry_date"] == expiry
    assert row["members"][0]["expiry_date"] == expiry
    assert row["status"] == "CURRENT"
    # Unknown/malformed anchors can never be projected as zero age or current.
    (tmp_path / "data" / file).write_text(json.dumps(data).replace('2026-09-16', 'not-a-date'))
    unknown = next(r for r in HORIZON.inventory(tmp_path, NOW) if r["gate"] == kind)
    assert unknown["status"] == "UNKNOWN"
    assert unknown["expiry_date"] == "UNKNOWN"


def test_css_clock_uses_source_members_and_nanosecond_order(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "assets/css-src").mkdir(parents=True)
    (tmp_path / "public/assets").mkdir(parents=True)
    (tmp_path / "src/css-build-freshness.mjs").write_text(
        'const COMMON = ["custom.css"];\n"site-custom.css": [...COMMON],\ncss_bundle_stale\npublic_css_stale')
    layer = tmp_path / "assets/css-src/custom.css"
    layer.write_text("a{}")
    ns = 1_790_381_000_000_000_000
    os.utime(layer, ns=(ns, ns))
    for name in ["site.css", "site-custom.css"]:
        for folder in ["assets", "public/assets"]:
            path = tmp_path / folder / name
            path.write_text("a{}")
            os.utime(path, ns=(ns, ns))
    assert all(row["status"] == "CURRENT" for row in HORIZON.css_gates(tmp_path))
    os.utime(layer, ns=(ns + 1, ns + 1))
    row = HORIZON.css_gates(tmp_path)[0]
    assert row["status"] == "STALE"
    assert row["source"] == "src/css-build-freshness.mjs:3"
    assert row["evidence_timestamp"] != "UNKNOWN"


def test_rendered_stamp_clock_current_stale_and_unknown(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "public/best").mkdir(parents=True)
    (tmp_path / "src/freshness.mjs").write_text('freshness_stamp_stale')
    page = tmp_path / "public/best/index.html"
    page.write_text('"dateModified":"2026-09-16" Updated 2026-09-16 Checked 2026-09-16')
    row = HORIZON.stamp_gate(tmp_path)
    assert row["source"] == "src/freshness.mjs:1"
    assert row["status"] == "CURRENT"
    page.write_text(page.read_text() + ' Checked 2026-09-17')
    assert HORIZON.stamp_gate(tmp_path)["status"] == "STALE"
    page.write_text('No known stamp')
    assert HORIZON.stamp_gate(tmp_path)["status"] == "UNKNOWN"


@pytest.mark.parametrize("mutation", ["new-file", "existing-file", "unfamiliar-name", "delete-file"])
def test_new_clock_mutation_cannot_escape_catalog(tmp_path, monkeypatch, mutation):
    (tmp_path / "src").mkdir()
    old = tmp_path / "src/known.mjs"
    old.write_text('export const existing = 1;')
    baseline = {"files": {"src/known.mjs": hashlib.sha256(old.read_bytes()).hexdigest()}}
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps(baseline))
    monkeypatch.setattr(HORIZON, "COVERAGE", coverage)
    monkeypatch.setattr(HORIZON, "inventory", lambda root, now: [])
    assert HORIZON.report(tmp_path, NOW)["verdict"] == "CURRENT"
    if mutation == "delete-file":
        old.unlink()
    else:
        path = old if mutation == "existing-file" else tmp_path / "src/new.mjs"
        path.write_text('if (Date.now() - Date.parse(record.captured) > 86400000 * 17) throw Error("nope");'
                        if mutation == "unfamiliar-name" else 'const MAX_AGE_DAYS = 17;\nif (age > MAX_AGE_DAYS) throw Error("new_clock_stale");')
    result = HORIZON.report(tmp_path, NOW)
    assert result["verdict"] == "UNKNOWN"
    assert result["coverage"]["unreviewed_sources"] == 1
    assert result["unknown"][0]["anchor_date"] == "UNKNOWN"
    assert result["unknown"][0]["source"].endswith(":1")


def test_source_window_change_is_extracted_not_hardcoded(tmp_path):
    (tmp_path / "src").mkdir()
    path = tmp_path / "src/price-tracker.mjs"
    path.write_text('const MAX_AGE_MS = 17 * 86_400_000;')
    assert HORIZON.window_constant(tmp_path, path.name, "MAX_AGE_MS") == 17
    path.write_text('const MAX_AGE_MS = getPolicy();')
    assert HORIZON.window_constant(tmp_path, path.name, "MAX_AGE_MS") is None


@pytest.mark.parametrize("window", [None, True, -1, "45", float("inf"), float("nan")])
def test_unknown_policy_never_means_fresh(tmp_path, window):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/gate.mjs").write_text('stale')
    row = HORIZON.gate(tmp_path, "gate", "gate.mjs", "stale", window,
                       [("one", "input.json", "checked_at", "2026-09-16")], NOW)
    assert row["status"] == "UNKNOWN"


def test_cli_malformed_data_is_unknown_json(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/program-registry.json").write_text('{broken')
    assert HORIZON.main(["--candidate", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "UNKNOWN"


def test_explicit_expiry_boundary_is_inclusive(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/deal-ingestion.mjs").write_text('nowMs < Date.parse(offer.expires_at)')
    row = HORIZON.gate(tmp_path, "deal-explicit-expiry", "deal-ingestion.mjs", 'nowMs <', 0,
                       [("one", "data/deals.json", "expires_at", NOW.isoformat())], NOW)
    assert row["status"] == "EXPIRED"
