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
            {"row_id": "one:typed", "price_kind": "floor_from", "evidence": [
                {"captured_at_utc": "2026-09-08T03:30:55Z"}], "identity_evidence": [
                {"captured_at_utc": "2026-09-08T03:30:55Z"}], "facts": {
                    "amount_usd": {"value": 100, "evidence": [
                        {"captured_at_utc": "2026-09-08T03:30:55Z"}]},
                    "period": {"value": "UNKNOWN", "evidence": []}}},
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


@pytest.mark.parametrize("mutation", [
    "remove-all-stamps", "missing-stamp", "invalid-stamp", "date-only", "non-utc",
    "missing-evidence", "empty-evidence", "missing-identity", "empty-identity",
    "missing-fact-evidence", "empty-fact-evidence", "invalid-witness",
])
def test_required_typed_witness_cannot_disappear(tmp_path, capsys, mutation):
    import copy
    import shutil
    real = os.environ.get("GLP270_REAL_CANDIDATE")
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    if real:
        real = Path(real)
        for name in ("src/pricing-evidence-v2.mjs", "scripts/validate-provider-pricing-matrix.mjs"):
            shutil.copyfile(real / name, tmp_path / name)
        matrix = json.loads((real / "data/provider-pricing-matrix.json").read_text())
        row = next(r for p in matrix["providers"].values() for r in p["medications"]
                   if r.get("row_id") == "altrx-glp1-us-cps:s32-0:compounded-semaglutide-injection")
    else:
        (tmp_path / "src/pricing-evidence-v2.mjs").write_text("evidence_older_than_recency_bar")
        (tmp_path / "scripts/validate-provider-pricing-matrix.mjs").write_text("const MAX_EVIDENCE_AGE_DAYS = 92;")
        witness = {"captured_at_utc": "2026-09-16T00:00:00Z", "quote": "synthetic"}
        row = {"row_id": "broken", "price_kind": "floor_from", "evidence": [copy.deepcopy(witness)],
               "identity_evidence": [copy.deepcopy(witness)], "facts": {
                   "amount_usd": {"value": 100, "evidence": [copy.deepcopy(witness)]},
                   "period": {"value": "month", "evidence": [copy.deepcopy(witness)]}}}
        healthy = copy.deepcopy(row)
        healthy["row_id"] = "healthy"
        matrix = {"providers": {"synthetic": {"medications": [healthy, row]}}}
    path = tmp_path / "data/provider-pricing-matrix.json"
    path.write_text(json.dumps(matrix))
    def native_errors():
        import subprocess
        code = ("import {validatePriceRow} from './src/pricing-evidence-v2.mjs';"
                "import {readFileSync} from 'node:fs';"
                "const m=JSON.parse(readFileSync(process.argv[1]));"
                "const r=Object.values(m.providers).flatMap(p=>p.medications).find(r=>r.row_id===process.argv[2]);"
                "console.log(JSON.stringify(validatePriceRow(r,{root:process.argv[3],now:Date.parse('2026-09-26T00:00:00Z')})));")
        run = subprocess.run(["node", "--input-type=module", "-e", code, str(path), row["row_id"], str(real)],
                             cwd=tmp_path, text=True, capture_output=True, check=True)
        return json.loads(run.stdout)
    if real:
        assert native_errors() == []
    def clock():
        return next(g for g in HORIZON.inventory(tmp_path, NOW) if g["gate"] == "pricing-typed-witness")
    assert clock()["status"] == "CURRENT"
    if mutation == "remove-all-stamps":
        def remove(node):
            if isinstance(node, dict):
                node.pop("captured_at_utc", None)
                for value in node.values():
                    remove(value)
            elif isinstance(node, list):
                for value in node:
                    remove(value)
        remove(row)
    elif mutation == "missing-stamp":
        row["evidence"][0].pop("captured_at_utc")
    elif mutation in ("invalid-stamp", "date-only", "non-utc"):
        row["evidence"][0]["captured_at_utc"] = {
            "invalid-stamp": "not-a-date", "date-only": "2026-09-16",
            "non-utc": "2026-09-16T00:00:00+01:00"}[mutation]
    elif mutation == "invalid-witness":
        row["evidence"][0] = None
    elif "fact" in mutation:
        fact = next(f for f in row["facts"].values() if f["value"] != "UNKNOWN")
        if mutation.startswith("missing"):
            fact.pop("evidence")
        else:
            fact["evidence"] = []
    else:
        key = "identity_evidence" if "identity" in mutation else "evidence"
        if mutation.startswith("missing"):
            row.pop(key)
        else:
            row[key] = []
    path.write_text(json.dumps(matrix))
    if real:
        errors = native_errors()
        assert errors
        if mutation in ("remove-all-stamps", "missing-stamp", "invalid-stamp", "date-only", "non-utc"):
            assert f"capture_date_invalid:{row['row_id']}" in errors
    result = clock()
    assert result["status"] == "UNKNOWN"
    assert result["row"] == row["row_id"]
    assert result["expiry_date"] == "UNKNOWN"
    assert HORIZON.main(["--candidate", str(tmp_path), "--now", NOW.isoformat()]) == 1
    reported = json.loads(capsys.readouterr().out)
    assert any(g["gate"] == "pricing-typed-witness" and g["row"] == row["row_id"]
               for g in reported["unknown"])


@pytest.mark.parametrize("field", ["calculator", "evidence_bindings"])
@pytest.mark.parametrize("value", [None, {}, []])
def test_dynamic_required_collection_missing_cannot_disappear(tmp_path, field, value):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src/provider-dynamic-profiles.mjs").write_text(
        'fail("calculator_evidence_stale"\nfail("evidence_binding_stale"')
    good = {"calculator": {"checked_as_of": "2026-09-16"},
            "evidence_bindings": [{"verified_as_of": "2026-09-16"}]}
    bad = {**good, field: value}
    path = tmp_path / "data/program-registry.json"
    path.write_text(json.dumps({"provider_dynamic_profiles": {"freshness_max_days": 90,
                               "profiles": {"healthy": good, "broken": bad}}}))
    name = "dynamic-calculator" if field == "calculator" else "dynamic-binding"
    result = next(g for g in HORIZON.inventory(tmp_path, NOW) if g["gate"] == name)
    assert result["status"] == "UNKNOWN"
    assert result["row"].startswith("broken")


@pytest.mark.parametrize("name", ["coupon-explicit-expiry", "deal-explicit-expiry", "deal-term-expiry", "promo-explicit-expiry"])
@pytest.mark.parametrize("value", ["MISSING", "", False, None])
def test_optional_expiry_only_native_absence_is_skipped(tmp_path, name, value):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    source, needle, file = {
        "coupon-explicit-expiry": ("coupon-offers.mjs", "nowDate >= new Date(expires.getTime() + day)", "coupon-offers.json"),
        "deal-explicit-expiry": ("deal-ingestion.mjs", "nowMs < Date.parse(offer.expires_at)", "deals.json"),
        "deal-term-expiry": ("deal-ingestion.mjs", "nowMs < Date.parse(term.expires_at)", "deals.json"),
        "promo-explicit-expiry": ("promo-code.mjs", "nowDate >= new Date(expiry.getTime() + dayMs())", "money-page-specs.json"),
    }[name]
    (tmp_path / "src" / source).write_text(needle)
    good = {"id": "healthy", "program_id": "healthy", "status": "VERIFIED", "expires_at": "2026-10-01"}
    bad = {**good, "id": "broken", "program_id": "broken", "expires_at": value}
    if value == "MISSING":
        bad.pop("expires_at")
    data = {"offers": [good, bad]}
    if name == "deal-term-expiry":
        data = {"offers": [{"id": "offer", "pricing": {"current_terms": [
            {**good, "label": "healthy"}, {**bad, "label": "broken"}]}}]}
    elif name == "promo-explicit-expiry":
        data = {"pages": [{"program_id": row["program_id"], "promo": row} for row in [good, bad]]}
    (tmp_path / "data" / file).write_text(json.dumps(data))
    result = next(g for g in HORIZON.inventory(tmp_path, NOW) if g["gate"] == name)
    optional = value is None or (name == "promo-explicit-expiry" and value == "MISSING")
    assert result["status"] == ("CURRENT" if optional else "UNKNOWN")
    if not optional:
        assert "broken" in result["row"]


@pytest.mark.parametrize("value", [None, "", False, "MISSING"])
def test_material_offer_missing_capture_is_not_fallback(tmp_path, monkeypatch, value):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/deal-ingestion.mjs").write_text(
        "const MAX_EVIDENCE_AGE_DAYS = 45;\nevaluated_at: receipt.fetched_at")
    bad = {"offer_id": "broken", "fetched_at": value}
    if value == "MISSING":
        bad.pop("fetched_at")
    monkeypatch.setattr(HORIZON, "qualification_clocks", lambda root: {
        "qualifications": {}, "material_offers": [
            {"offer_id": "healthy", "fetched_at": "2026-09-16"}, bad]})
    result = next(g for g in HORIZON.inventory(tmp_path, NOW) if g["gate"] == "deal-material-offer")
    assert result["status"] == "UNKNOWN"
    assert result["row"] == "broken"


def test_cache_loader_retains_missing_and_invalid_dates_but_not_null(tmp_path):
    # Run the actual JS projection with a pure, isolated accepted-input loader.
    (tmp_path / "data").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data/wave4-source-capture-ledger.json").write_text("{}")
    (tmp_path / "scripts/glp_evidence_ledger.mjs").write_text('''
export const loadEvidenceInputs = () => ({ledger: {records: [
 {capture_id:'healthy',cached_at:'2026-09-16'}, {capture_id:'missing'},
 {capture_id:'empty',cached_at:''}, {capture_id:'null',cached_at:null}
]},config:{}});
export const materialQualificationsFor = () => [];
export const materialOffersFor = () => [];
''')
    result = HORIZON.qualification_clocks(tmp_path, tmp_path)
    assert result is not None
    assert result["ledger_cached"] == [
        {"id": "healthy", "cached_at": "2026-09-16"},
        {"id": "missing", "cached_at": None}, {"id": "empty", "cached_at": ""}]
