#!/usr/bin/env python3
"""Enumerate EVERY evidence-freshness gate in the candidate and when each one expires.

WHY: GLP-30 spent a week being blocked one gate at a time. The candidate's evidence was captured
around 2026-07-31..08-02 and carries at least nine INDEPENDENT freshness clocks with 30/31/45/90-day
windows, so they come due in sequence: each build clears one gate and immediately fails the next.
Fixing them one per build is unbounded; this makes the set finite and orders it by deadline.

Read-only. Names each gate, its window, its worst row, and the hours remaining.
"""
from __future__ import annotations

import json
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

CAND = Path("/home/ubuntu/business/site-system/flagship-v2/candidates/glp34-provider-families-20260912")

def moment(value):
    if not isinstance(value, str):
        return None
    try:
        text = value + "T00:00:00+00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def source_line(root, file, needle):
    relative = file if file.startswith("scripts/") else f"src/{file}"
    path = root / relative
    if path.is_file():
        for line, text in enumerate(path.read_text().splitlines(), 1):
            if needle in text and not text.lstrip().startswith(("//", "*")):
                return f"{relative}:{line}"
    return f"{relative}:UNKNOWN"


def window_constant(root, file, name):
    path = root / (file if file.startswith("scripts/") else f"src/{file}")
    if not path.is_file():
        return None
    match = re.search(rf"\bconst {name}\s*=\s*(\d+)(?:\s*\*\s*(?:DAY_MS|86_400_000))?\s*;", path.read_text())
    return int(match.group(1)) if match else None


def gate(root, name, source_file, needle, window, records, now, *, unit="days", clock=None):
    """records are (identity, evidence file, field, timestamp); missing data is UNKNOWN."""
    source = source_line(root, source_file, needle)
    result = {"gate": name, "source": source, "window_days": window if unit == "days" else None,
              "window_hours": window if unit == "hours" else None,
              "evidence_timestamp": "UNKNOWN", "expiry_date": "UNKNOWN", "status": "UNKNOWN"}
    if source.endswith(":UNKNOWN") or window is None or not records:
        return result
    observed = []
    for who, file, field, timestamp in records:
        parsed = moment(timestamp)
        if parsed is None:
            return {**result, "row": who, "evidence_file": file, "stamp_field": field}
        expiry = parsed + timedelta(**{unit: window})
        observed.append((expiry, who, file, field, timestamp))
    expiry, who, file, field, timestamp = min(observed)
    checked = clock or now
    return {**result, "row": who, "evidence_file": file, "stamp_field": field,
            "evidence_timestamp": timestamp, "expiry_date": expiry.date().isoformat(),
            "expiry_at_utc": expiry.isoformat(), "hours_left": round((expiry - checked).total_seconds() / 3600, 1),
            "status": "EXPIRED" if checked > expiry else "CURRENT",
            "evaluation_clock": "historical_as_of" if clock else "wall_clock",
            "member_expiries": {d: sum(1 for item in observed if item[0].date().isoformat() == d)
                                for d in sorted({item[0].date().isoformat() for item in observed})}}


def document(root, name):
    path = root / "data" / name
    return json.loads(path.read_text()) if path.is_file() else {}


def qualification_clocks(root):
    """Read the site's hash-bound adopted evidence, never an unaccepted proposal."""
    if not (root / "data" / "wave4-source-capture-ledger.json").is_file():
        return {"qualifications": {}, "material_offers": []}
    code = ("import {loadEvidenceInputs,materialQualificationsFor,materialOffersFor} from './scripts/glp_evidence_ledger.mjs';"
            "const x=loadEvidenceInputs(process.argv[1]);"
            "console.log(JSON.stringify({ledger_generated_at:x.ledger.generated_at_utc,ledger_path:x.ledger_path,"
            "ledger_cached:x.ledger.records.filter(r=>r.cached_at).map(r=>({id:r.capture_id,cached_at:r.cached_at})),"
            "config:x.config,config_path:x.config_path,"
            "qualifications:materialQualificationsFor(x.ledger).map(q=>"
            "({capture_id:q.material?.capture_id,observation_id:q.material?.observation_id,fetched_at:q.fetched_at})),"
            "material_offers:materialOffersFor(x).map(q=>({offer_id:q.material?.offer_id,fetched_at:q.fetched_at}))}));")
    try:
        run = subprocess.run(["node", "--input-type=module", "-e", code, str(root)],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        if run.returncode:
            return None
        data = json.loads(run.stdout)
        data["qualifications"] = {r["capture_id"]: r["fetched_at"] for r in data["qualifications"] if r.get("capture_id")}
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def css_gates(root):
    src = root / "src" / "css-build-freshness.mjs"
    css = root / "assets" / "css-src"
    layers = sorted(css.glob("*.css")) if css.is_dir() else []
    bundle_map = {"site.css": [p.name for p in layers]}
    if src.is_file():
        for line in src.read_text().splitlines():
            found = re.match(r'\s*"(site-[^" ]+\.css)": \[(.*)\]', line)
            if not found:
                continue
            names = re.findall(r'"([\w-]+\.css)"', found.group(2))
            if "...COMMON" in found.group(2):
                names = ["00-foundation.css", "01-primitives.css", "02-shell.css", *names]
            if "ACTION_AURORA" in found.group(2):
                names.append("99-action-aurora.css")
            bundle_map[found.group(1)] = names
    entries = []
    public_entries = []
    missing_layers = [] if css.is_dir() else ["assets/css-src/"]
    missing_public = []
    for bundle, members in bundle_map.items():
        built = root / "assets" / bundle
        public = root / "public" / "assets" / bundle
        if not built.is_file():
            missing_layers.append(f"assets/{bundle}")
            missing_public.append(f"assets/{bundle}")
        if not public.is_file():
            missing_public.append(f"public/assets/{bundle}")
        for name in members:
            layer = css / name
            if not layer.is_file():
                missing_layers.append(f"assets/css-src/{name}")
            if layer.is_file() and built.is_file():
                entries.append((layer.stat().st_mtime, built.stat().st_mtime, f"assets/css-src/{name}", bundle))
        if built.is_file() and public.is_file():
            drift = hashlib.sha256(built.read_bytes()).digest() != hashlib.sha256(public.read_bytes()).digest()
            public_entries.append((built.stat().st_mtime, public.stat().st_mtime, f"assets/{bundle}", bundle, drift))

    def result(name, needle, pairs, missing):
        item = {"gate": name, "source": source_line(root, "css-build-freshness.mjs", needle),
                "window": "source mtime <= bundle mtime" if name == "css-bundle" else "bundle mtime <= public mtime",
                "evidence_timestamp": "UNKNOWN", "expiry_date": "UNKNOWN", "status": "UNKNOWN"}
        if missing:
            return {**item, "row": missing[0], "evidence_file": missing[0],
                    "missing_required_paths": sorted(set(missing))}
        if not pairs or item["source"].endswith(":UNKNOWN"):
            return item
        pair = max(pairs, key=lambda row: (row[0] > row[1] or (len(row) > 4 and row[4]), row[0] - row[1]))
        observed = datetime.fromtimestamp(pair[0], timezone.utc)
        stale = pair[0] > pair[1] or (len(pair) > 4 and pair[4])
        return {**item, "row": pair[3], "evidence_file": pair[2],
                "evidence_timestamp": observed.isoformat(), "status": "STALE" if stale else "CURRENT",
                "expiry_date": observed.date().isoformat() if pair[0] > pair[1] else "UNKNOWN",
                "hash_drift": bool(len(pair) > 4 and pair[4])}

    return [result("css-bundle", "css_bundle_stale", entries, missing_layers),
            result("public-css", "public_css_stale", public_entries, missing_public)]


def stamp_gate(root):
    result = {"gate": "rendered-freshness-stamp", "source": source_line(root, "freshness.mjs", "freshness_stamp_stale"),
              "window": "dateModified >= newest rendered evidence date", "evidence_timestamp": "UNKNOWN",
              "expiry_date": "UNKNOWN", "status": "UNKNOWN"}
    candidates = []
    for family in ("best", "cost", "compare", "reviews"):
        folder = root / "public" / family
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.html"):
            html = path.read_text(errors="replace")
            stamp = re.search(r'"dateModified":"(\d{4}-\d{2}-\d{2})', html)
            if not stamp:
                candidates.append((None, None, path))
                continue
            body = html.replace(stamp.group(0), "").replace(f"Updated {stamp.group(1)}", "")
            body = re.sub(r'"datePublished":"\d{4}-\d{2}-\d{2}"', "", body)
            dates = re.findall(r'(?<![\d>])(\d{4}-\d{2}-\d{2})(?![\d])', body)
            candidates.append((stamp.group(1), max(dates) if dates else stamp.group(1), path))
    if not candidates or result["source"].endswith(":UNKNOWN"):
        return result
    invalid = [x for x in candidates if x[0] is None or x[0] < x[1]]
    stamp, newest, path = invalid[0] if invalid else min(candidates)
    return {**result, "row": str(path.relative_to(root)), "evidence_file": str(path.relative_to(root)),
            "stamp_field": "dateModified", "stamp": stamp or "UNKNOWN",
            "evidence_timestamp": newest or "UNKNOWN", "status": "STALE" if invalid else "CURRENT",
            "expiry_date": newest if invalid and newest else "UNKNOWN"}


def inventory(root=CAND, now=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    registry = document(root, "program-registry.json")
    taxonomy = registry.get("therapeutic_taxonomy", {})
    consumer = registry.get("consumer_therapeutic_authorities", {})
    profiles = registry.get("provider_dynamic_profiles", {})
    offers = document(root, "coupon-offers.json")
    study = document(root, "original-data-study.json")
    prices = document(root, "price-tracker.json")
    mystery = document(root, "mystery-shop.json")
    deals = document(root, "deals.json")
    money = document(root, "money-page-specs.json")
    ledger = document(root, "wave4-source-capture-ledger.json")
    rfile = "data/program-registry.json"
    findings = []

    def add(name, src, needle, window, rows, **kwargs):
        findings.append(gate(root, name, src, needle, window, rows, now, **kwargs))

    programs = taxonomy.get("programs", {})
    add("therapeutic-taxonomy", "therapeutic-taxonomy.mjs", 'fail("evidence_stale"', taxonomy.get("freshness_max_days"),
        [(pid, rfile, "verified_as_of", row.get("verified_as_of")) for pid, row in programs.items()])
    retatrutide = consumer.get("molecules", {}).get("retatrutide", {})
    add("consumer-authority", "therapeutic-taxonomy.mjs", 'fail("consumer_authority_stale"', consumer.get("freshness_max_days"),
        [(row.get("authority"), rfile, "verified_as_of", row.get("verified_as_of"))
         for row in retatrutide.get("official_evidence", [])])
    audit = retatrutide.get("local_capture_authority", {}).get("audit_summary", {})
    add("consumer-capture", "therapeutic-taxonomy.mjs", 'fail("consumer_authority_capture_status_time_invalid"',
        consumer.get("freshness_max_days"),
        [("retatrutide", audit.get("path", rfile), "audited_at", audit.get("audited_at"))] if audit else [])
    dynamic = list(profiles.get("profiles", {}).items())
    add("dynamic-profile", "provider-dynamic-profiles.mjs", 'fail("evidence_stale"', profiles.get("freshness_max_days"),
        [(pid, rfile, "verified_as_of", row.get("verified_as_of")) for pid, row in dynamic])
    add("dynamic-calculator", "provider-dynamic-profiles.mjs", 'fail("calculator_evidence_stale"', profiles.get("freshness_max_days"),
        [(pid, rfile, "calculator.checked_as_of", row.get("calculator", {}).get("checked_as_of"))
         for pid, row in dynamic if row.get("calculator")])
    add("dynamic-binding", "provider-dynamic-profiles.mjs", 'fail("evidence_binding_stale"', profiles.get("freshness_max_days"),
        [(f"{pid}:{i}", rfile, "evidence_bindings.verified_as_of", row.get("verified_as_of"))
         for pid, profile in dynamic for i, row in enumerate(profile.get("evidence_bindings", []))])
    add("coupon-offers", "coupon-offers.mjs", "nowDate.getTime() - verified.getTime() > feed.source_contract.max_age_days", offers.get("source_contract", {}).get("max_age_days"),
        [(row.get("program_id"), "data/coupon-offers.json", "verified_at", row.get("verified_at"))
         for row in offers.get("offers", []) if row.get("status") == "VERIFIED"])
    add("coupon-explicit-expiry", "coupon-offers.mjs", "nowDate >= new Date(expires.getTime() + day)", 1,
        [(row.get("program_id"), "data/coupon-offers.json", "expires_at", row.get("expires_at"))
         for row in offers.get("offers", []) if row.get("status") == "VERIFIED" and row.get("expires_at")])
    add("original-study", "original-data.mjs", 'throw new Error("study_source_stale")',
        study.get("methodology", {}).get("freshness_max_age_hours"),
        [(row.get("source_id"), "data/original-data-study.json", "checked_at", row.get("checked_at"))
         for row in study.get("sources", [])], unit="hours", clock=moment(study.get("as_of_utc")))
    preserved = root / "artifacts" / "ops" / "wave4-deal-ingestion-manifest.json"
    frozen = moment(json.loads(preserved.read_text()).get("evaluated_at")) if preserved.is_file() else None
    authority = qualification_clocks(root)
    material_offers = {row["offer_id"]: row["fetched_at"] for row in (authority or {}).get("material_offers", [])
                       if row.get("offer_id") and row.get("fetched_at")}
    add("deals", "deal-ingestion.mjs", "offer_evidence_stale", window_constant(root, "deal-ingestion.mjs", "MAX_EVIDENCE_AGE_DAYS"),
        [(row.get("id"), "data/deals.json", "checked_at", row.get("checked_at"))
         for row in deals.get("offers", []) if row.get("status") == "VERIFIED" and row.get("id") not in material_offers], clock=frozen)
    add("deal-material-offer", "deal-ingestion.mjs", "evaluated_at: receipt.fetched_at",
        window_constant(root, "deal-ingestion.mjs", "MAX_EVIDENCE_AGE_DAYS"),
        [(offer_id, "ops/evidence-freshness/material-acceptances.jsonl", "fetched_at", fetched_at)
         for offer_id, fetched_at in material_offers.items()] if authority is not None else [])
    add("deal-explicit-expiry", "deal-ingestion.mjs", "nowMs < Date.parse(offer.expires_at)", 0,
        [(row.get("id"), "data/deals.json", "expires_at", row.get("expires_at"))
         for row in deals.get("offers", []) if row.get("status") == "VERIFIED" and row.get("expires_at")], clock=frozen)
    add("deal-term-expiry", "deal-ingestion.mjs", "nowMs < Date.parse(term.expires_at)", 0,
        [(f"{row.get('id')}:{term.get('label')}", "data/deals.json", "pricing.expires_at", term.get("expires_at"))
         for row in deals.get("offers", []) for group in row.get("pricing", {}).values() if isinstance(group, list)
         for term in group if isinstance(term, dict) and term.get("expires_at")], clock=frozen)
    max_mystery = window_constant(root, "mystery-shop.mjs", "MAX_AGE_MS")
    add("mystery-ledger", "mystery-shop.mjs", '"evidence_ledger_generated_at"', max_mystery,
        [("ledger", (authority or {}).get("ledger_path", "data/wave4-source-capture-ledger.json"), "generated_at_utc",
          (authority or {}).get("ledger_generated_at"))] if ledger else [])
    add("mystery-capture-cache", "mystery-shop.mjs", '"capture_cached_at"', max_mystery,
        [(row["id"], (authority or {}).get("ledger_path", "data/wave4-source-capture-ledger.json"), "cached_at", row["cached_at"])
         for row in (authority or {}).get("ledger_cached", [])] if authority is not None else [])
    qualified = (authority or {}).get("qualifications", {})
    active_mystery = (authority or {}).get("config", mystery)
    add("mystery-observation", "mystery-shop.mjs", 'parseFreshInstant(qualification ? qualification.fetched_at : row.observed_at', max_mystery,
        [(row.get("id"), (authority or {}).get("config_path", "data/mystery-shop.json"),
          "qualified.fetched_at" if qualified and row.get("capture_id") in qualified else "observed_at",
          qualified.get(row.get("capture_id")) if qualified and row.get("capture_id") in qualified else row.get("observed_at"))
         for row in active_mystery.get("observations", [])] if authority is not None else [])
    add("promo-code", "promo-code.mjs", "promo_evidence_stale", window_constant(root, "promo-code.mjs", "PROMO_MAX_AGE_DAYS"),
        [(row.get("program_id"), "data/money-page-specs.json", "promo.verified_as_of", row["promo"].get("verified_as_of"))
         for row in money.get("pages", []) if row.get("promo", {}).get("status") == "VERIFIED"])
    add("promo-explicit-expiry", "promo-code.mjs", "nowDate >= new Date(expiry.getTime() + dayMs())", 1,
        [(row.get("program_id"), "data/money-page-specs.json", "promo.expires_at", row["promo"].get("expires_at"))
         for row in money.get("pages", []) if row.get("promo", {}).get("status") == "VERIFIED" and row["promo"].get("expires_at")])
    max_price = window_constant(root, "price-tracker.mjs", "MAX_AGE_MS")
    add("price-tracker", "price-tracker.mjs", "tracker_freshness_invalid", max_price,
        [(row.get("id"), "data/price-tracker.json",
          "qualified.fetched_at" if qualified and row.get("source_capture_id") in qualified else "checked_at",
          qualified.get(row.get("source_capture_id")) if qualified and row.get("source_capture_id") in qualified else row.get("checked_at"))
         for row in prices.get("records", [])] if authority is not None else [])
    add("price-alert", "price-tracker.mjs", "alert_record_freshness_invalid", max_price, [])
    matrix = document(root, "provider-pricing-matrix.json")
    pricing_window = window_constant(root, "scripts/validate-provider-pricing-matrix.mjs", "MAX_EVIDENCE_AGE_DAYS")
    typed_witnesses = []
    legacy_rows = []
    for program, provider in matrix.get("providers", {}).items():
        for row in provider.get("medications", []):
            identity = row.get("row_id") or f"{program}:{row.get('medication_id')}"
            if "price_kind" in row:
                def witnesses(node):
                    if isinstance(node, dict):
                        if "captured_at_utc" in node:
                            yield node
                        for child in node.values():
                            yield from witnesses(child)
                    elif isinstance(node, list):
                        for child in node:
                            yield from witnesses(child)
                typed_witnesses.extend(
                    (identity, "data/provider-pricing-matrix.json", "captured_at_utc", witness.get("captured_at_utc"))
                    for witness in witnesses(row)
                )
            else:
                captured = row.get("captured_on")
                legacy_rows.append((identity, "data/provider-pricing-matrix.json", "captured_on@12:00Z",
                                    f"{captured}T12:00:00Z" if captured else None))
    add("pricing-typed-witness", "pricing-evidence-v2.mjs", "evidence_older_than_recency_bar", pricing_window, typed_witnesses)
    add("pricing-legacy-capture", "scripts/validate-provider-pricing-matrix.mjs", "ageInDays(row.captured_on, now) > MAX_EVIDENCE_AGE_DAYS", pricing_window, legacy_rows)
    findings.extend(css_gates(root))
    findings.append(stamp_gate(root))
    return findings


def main() -> int:
    now = datetime.now(timezone.utc)
    findings = inventory(CAND, now)
    findings.sort(key=lambda f: (f.get("hours_left", float("inf")), f["gate"]))
    expired = [f for f in findings if f["status"] in ("EXPIRED", "STALE")]
    unknown = [f for f in findings if f["status"] == "UNKNOWN"]
    soon = [f for f in findings if f.get("evaluation_clock") == "wall_clock"
            and 0 <= f.get("hours_left", float("inf")) <= 72]
    verdict = "STALE" if expired else "UNKNOWN" if unknown else "CURRENT"
    print(json.dumps({"schema": "candidate-freshness-horizon-v2", "observed_at": now.isoformat(),
                      "verdict": verdict,
                      "gates": len(findings), "expired": expired, "unknown": unknown,
                      "due_within_72h": soon, "all": findings}, indent=2))
    return 1 if expired or unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
