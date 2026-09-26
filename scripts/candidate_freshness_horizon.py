#!/usr/bin/env python3
"""Enumerate EVERY evidence-freshness gate in the candidate and when each one expires.

WHY: GLP-30 spent a week being blocked one gate at a time. The candidate's evidence was captured
around 2026-07-31..08-02 and carries at least nine INDEPENDENT freshness clocks with 30/31/45/90-day
windows, so they come due in sequence: each build clears one gate and immediately fails the next.
Fixing them one per build is unbounded; this makes the set finite and orders it by deadline.

Read-only. Names each gate, its window, its worst row, and the hours remaining.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import math
import sys
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
    if (source.endswith(":UNKNOWN") or isinstance(window, bool)
            or not isinstance(window, (int, float)) or not math.isfinite(window)
            or window < 0 or not records):
        return result
    observed = []
    for who, file, field, timestamp in records:
        parsed = moment(timestamp)
        if parsed is None:
            return {**result, "row": who, "evidence_file": file, "stamp_field": field}
        tolerance = timedelta(days=1) if source_file in ("therapeutic-taxonomy.mjs", "provider-dynamic-profiles.mjs") else timedelta(0)
        if not name.endswith("expiry") and parsed > (clock or now) + tolerance:
            return {**result, "row": who, "evidence_file": file, "stamp_field": field,
                    "reason": "future_anchor", "evidence_timestamp": timestamp}
        try:
            expiry = parsed + timedelta(**{unit: window})
        except OverflowError:
            return {**result, "row": who, "reason": "window_overflow"}
        observed.append((expiry, who, file, field, timestamp))
    expiry, who, file, field, timestamp = min(observed)
    checked = clock or now
    return {**result, "row": who, "evidence_file": file, "stamp_field": field,
            "evidence_timestamp": timestamp, "expiry_date": expiry.date().isoformat(),
            "expiry_at_utc": expiry.isoformat(), "hours_left": round((expiry - checked).total_seconds() / 3600, 1),
            "status": "EXPIRED" if (checked >= expiry if name.endswith("expiry") else checked > expiry) else "CURRENT",
            "expiry_boundary": "inclusive" if name.endswith("expiry") else "exclusive",
            "evaluated_at_utc": checked.isoformat(),
            "evaluation_clock": "historical_as_of" if clock else "wall_clock",
            "members": [{"row": who, "anchor_date": timestamp, "expiry_at_utc": exp.isoformat(),
                         "expiry_date": exp.date().isoformat(), "evidence_file": file, "stamp_field": field}
                        for exp, who, file, field, timestamp in observed],
            "member_expiries": {d: sum(1 for item in observed if item[0].date().isoformat() == d)
                                for d in sorted({item[0].date().isoformat() for item in observed})}}


def document(root, name):
    path = root / "data" / name
    return json.loads(path.read_text()) if path.is_file() else {}


def qualification_clocks(root, authority_root=None):
    """Read the site's hash-bound adopted evidence, never an unaccepted proposal."""
    if not (root / "data" / "wave4-source-capture-ledger.json").is_file():
        return {"qualifications": {}, "material_offers": []}
    authority_root = Path(authority_root) if authority_root else Path(__file__).resolve().parents[1]
    code = ("import {loadEvidenceInputs,materialQualificationsFor,materialOffersFor} from './scripts/glp_evidence_ledger.mjs';"
            "const x=loadEvidenceInputs(process.argv[1]);"
            "console.log(JSON.stringify({ledger_generated_at:x.ledger.generated_at_utc,ledger_path:x.ledger_path,"
            "ledger_cached:x.ledger.records.filter(r=>r.cached_at!==null).map(r=>({id:r.capture_id,cached_at:r.cached_at??null})),"
            "config:x.config,config_path:x.config_path,"
            "qualifications:materialQualificationsFor(x.ledger).map(q=>"
            "({capture_id:q.material?.capture_id,observation_id:q.material?.observation_id,fetched_at:q.fetched_at})),"
            "material_offers:materialOffersFor(x).map(q=>({offer_id:q.material?.offer_id,fetched_at:q.fetched_at}))}));")
    try:
        run = subprocess.run(["node", "--input-type=module", "-e", code, str(root)],
                             cwd=authority_root, capture_output=True, text=True, timeout=30)
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
        text = src.read_text()
        constants = {}
        for key, value in re.findall(r'const\s+(\w+)\s*=\s*(\[[^;]*\]|"[^"\n]*")\s*;', text):
            constants[key] = re.findall(r'"([\w-]+\.css)"', value)
        for line in text.splitlines():
            found = re.match(r'\s*"(site-[^" ]+\.css)": \[(.*)\]', line)
            if not found:
                continue
            names = re.findall(r'"([\w-]+\.css)"', found.group(2))
            for token in re.sub(r'"[^"]*"', '', found.group(2)).replace('...', '').split(','):
                if token.strip():
                    names.extend(constants.get(token.strip(), [f'UNKNOWN-{token.strip()}']))
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
                entries.append((layer.stat().st_mtime_ns, built.stat().st_mtime_ns, f"assets/css-src/{name}", bundle))
        if built.is_file() and public.is_file():
            drift = hashlib.sha256(built.read_bytes()).digest() != hashlib.sha256(public.read_bytes()).digest()
            public_entries.append((built.stat().st_mtime_ns, public.stat().st_mtime_ns, f"assets/{bundle}", bundle, drift))

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
        observed = datetime.fromtimestamp(pair[0] / 1_000_000_000, timezone.utc)
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
            "evidence_timestamp": newest or "UNKNOWN",
            "status": "UNKNOWN" if stamp is None else "STALE" if invalid else "CURRENT",
            "expiry_date": newest if invalid and newest else "UNKNOWN"}


def required_items(value):
    """A required nonempty collection keeps an UNKNOWN slot when absent/malformed."""
    return value if isinstance(value, list) and value else [None]


def field(node, name):
    return node.get(name) if isinstance(node, dict) else None


def typed_capture_records(row, identity):
    """Enumerate native witness positions, never discover them by timestamp keys.

    pricing-evidence-v2 requires row/identity evidence and known-fact evidence.
    UNKNOWN facts may have an empty evidence array, but not a missing array.
    """
    collections = [(name, required_items(row.get(name)))
                   for name in ("evidence", "identity_evidence")]
    facts = row.get("facts")
    if not isinstance(facts, dict) or not facts:
        collections.append(("facts", [None]))
    else:
        for name, fact in facts.items():
            evidence = field(fact, "evidence")
            if field(fact, "value") == "UNKNOWN" and isinstance(evidence, list):
                collections.append((f"facts.{name}.evidence", evidence))
            else:
                collections.append((f"facts.{name}.evidence", required_items(evidence)))
    for position, witnesses in collections:
        for index, witness in enumerate(witnesses):
            stamp = field(witness, "captured_at_utc")
            # Native validDate requires a full UTC instant, not merely any ISO date.
            if not isinstance(stamp, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", stamp):
                stamp = None
            yield (identity, "data/provider-pricing-matrix.json",
                   f"{position}[{index}].captured_at_utc", stamp)


def inventory(root=CAND, now=None, authority_root=None):
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
        [(pid, rfile, "calculator.checked_as_of", field(row.get("calculator"), "checked_as_of"))
         for pid, row in dynamic])
    add("dynamic-binding", "provider-dynamic-profiles.mjs", 'fail("evidence_binding_stale"', profiles.get("freshness_max_days"),
        [(f"{pid}:{i}", rfile, "evidence_bindings.verified_as_of", field(row, "verified_as_of"))
         for pid, profile in dynamic for i, row in enumerate(required_items(profile.get("evidence_bindings")))])
    add("coupon-offers", "coupon-offers.mjs", "nowDate.getTime() - verified.getTime() > feed.source_contract.max_age_days", offers.get("source_contract", {}).get("max_age_days"),
        [(row.get("program_id"), "data/coupon-offers.json", "verified_at", row.get("verified_at"))
         for row in offers.get("offers", []) if row.get("status") == "VERIFIED"])
    add("coupon-explicit-expiry", "coupon-offers.mjs", "nowDate >= new Date(expires.getTime() + day)", 1,
        [(row.get("program_id"), "data/coupon-offers.json", "expires_at", row.get("expires_at"))
         for row in offers.get("offers", []) if row.get("status") == "VERIFIED" and ("expires_at" not in row or row["expires_at"] is not None)])
    add("original-study", "original-data.mjs", 'throw new Error("study_source_stale")',
        study.get("methodology", {}).get("freshness_max_age_hours"),
        [(row.get("source_id"), "data/original-data-study.json", "checked_at", row.get("checked_at"))
         for row in study.get("sources", [])], unit="hours", clock=moment(study.get("as_of_utc")))
    preserved = root / "artifacts" / "ops" / "wave4-deal-ingestion-manifest.json"
    frozen = moment(json.loads(preserved.read_text()).get("evaluated_at")) if preserved.is_file() else None
    authority = qualification_clocks(root, authority_root) if authority_root else qualification_clocks(root)
    material_offers = {row["offer_id"]: row.get("fetched_at") for row in (authority or {}).get("material_offers", [])
                       if row.get("offer_id")}
    add("deals", "deal-ingestion.mjs", "offer_evidence_stale", window_constant(root, "deal-ingestion.mjs", "MAX_EVIDENCE_AGE_DAYS"),
        [(row.get("id"), "data/deals.json", "checked_at", row.get("checked_at"))
         for row in deals.get("offers", []) if row.get("status") == "VERIFIED" and row.get("id") not in material_offers], clock=frozen)
    add("deal-material-offer", "deal-ingestion.mjs", "evaluated_at: receipt.fetched_at",
        window_constant(root, "deal-ingestion.mjs", "MAX_EVIDENCE_AGE_DAYS"),
        [(offer_id, "ops/evidence-freshness/material-acceptances.jsonl", "fetched_at", fetched_at)
         for offer_id, fetched_at in material_offers.items()] if authority is not None else [])
    add("deal-explicit-expiry", "deal-ingestion.mjs", "nowMs < Date.parse(offer.expires_at)", 0,
        [(row.get("id"), "data/deals.json", "expires_at", row.get("expires_at"))
         for row in deals.get("offers", []) if row.get("status") == "VERIFIED" and ("expires_at" not in row or row["expires_at"] is not None)], clock=frozen)
    add("deal-term-expiry", "deal-ingestion.mjs", "nowMs < Date.parse(term.expires_at)", 0,
        [(f"{row.get('id')}:{term.get('label')}", "data/deals.json", "pricing.expires_at", term.get("expires_at"))
         for row in deals.get("offers", []) for group in row.get("pricing", {}).values() if isinstance(group, list)
         for term in group if isinstance(term, dict) and ("expires_at" not in term or term["expires_at"] is not None)], clock=frozen)
    max_mystery = window_constant(root, "mystery-shop.mjs", "MAX_AGE_MS")
    # A second ledger timestamp is independently enforced in the config.
    active_mystery = (authority or {}).get("config", mystery)
    add("mystery-config-ledger", "mystery-shop.mjs", '"evidence_ledger_generated_at"', max_mystery,
        [("config", (authority or {}).get("config_path", "data/mystery-shop.json"),
          "source_capture_ledger.generated_at_utc",
          active_mystery.get("source_capture_ledger", {}).get("generated_at_utc"))])
    add("mystery-ledger", "mystery-shop.mjs", '"capture_ledger_generated_at"', max_mystery,
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
         for row in money.get("pages", []) if row.get("promo", {}).get("status") == "VERIFIED" and row["promo"].get("expires_at") is not None])
    max_price = window_constant(root, "price-tracker.mjs", "MAX_AGE_MS")
    add("price-tracker", "price-tracker.mjs", "tracker_freshness_invalid", max_price,
        [(row.get("id"), "data/price-tracker.json",
          "qualified.fetched_at" if qualified and row.get("source_capture_id") in qualified else "checked_at",
          qualified.get(row.get("source_capture_id")) if qualified and row.get("source_capture_id") in qualified else row.get("checked_at"))
         for row in prices.get("records", [])] if authority is not None else [])
    add("price-alert", "price-tracker.mjs", "alert_record_freshness_invalid", max_price, [])
    add("mystery-renewal", "mystery-shop.mjs", '"renewal_qualification"', max_mystery,
        [(capture_id, "accepted evidence ledger", "fetched_at", stamp) for capture_id, stamp in qualified.items()])
    for name, source, needle, window, rows, id_field, stamp_field in (
        ("mystery-lineage", "mystery-shop.mjs", "observation_qualification_lineage_invalid", max_mystery,
         active_mystery.get("observations", []), "capture_id", "observed_at"),
        ("price-lineage", "price-tracker.mjs", "tracker_qualification_lineage_invalid", max_price,
         prices.get("records", []), "source_capture_id", "checked_at"),
    ):
        members = []
        for row in rows:
            if row.get(id_field) in qualified:
                member = gate(root, name, source, needle, window,
                              [(row.get("id"), "qualified input", stamp_field, row.get(stamp_field))], now,
                              clock=moment(qualified[row[id_field]]))
                member["evaluation_clock"] = "qualification_lineage"
                members.append(member)
        if members:
            worst = min(members, key=lambda item: (item["status"] != "UNKNOWN", item.get("hours_left", float("inf"))))
            findings.append({**worst, "lineage_members": members})
        else:
            add(name, source, needle, window, [])
    matrix = document(root, "provider-pricing-matrix.json")
    pricing_window = window_constant(root, "scripts/validate-provider-pricing-matrix.mjs", "MAX_EVIDENCE_AGE_DAYS")
    typed_witnesses = []
    legacy_rows = []
    for program, provider in matrix.get("providers", {}).items():
        for row in provider.get("medications", []):
            identity = row.get("row_id") or f"{program}:{row.get('medication_id')}"
            if "price_kind" in row:
                typed_witnesses.extend(typed_capture_records(row, identity))
            else:
                captured = row.get("captured_on")
                legacy_rows.append((identity, "data/provider-pricing-matrix.json", "captured_on@12:00Z",
                                    f"{captured}T12:00:00Z" if captured else None))
    add("pricing-typed-witness", "pricing-evidence-v2.mjs", "evidence_older_than_recency_bar", pricing_window, typed_witnesses)
    add("pricing-legacy-capture", "scripts/validate-provider-pricing-matrix.mjs", "ageInDays(row.captured_on, now) > MAX_EVIDENCE_AGE_DAYS", pricing_window, legacy_rows)
    findings.extend(css_gates(root))
    findings.append(stamp_gate(root))
    if moment(study.get("as_of_utc")) is None:
        original = next(row for row in findings if row["gate"] == "original-study")
        original.update(status="UNKNOWN", reason="unknown_evaluation_clock", evidence_timestamp="UNKNOWN", expiry_date="UNKNOWN")
    return findings


COVERAGE = Path(__file__).with_name("candidate_freshness_coverage.json")


def source_coverage(root, baseline=None):
    """Fail closed on ANY new/changed src file, not just familiar clock names.

    Arbitrary JS cannot be exhaustively inferred with regex. The extraction below
    is a reviewed adapter catalog. This conservative byte census is its coverage
    guard: a newly added clock even with an unfamiliar name cannot pass unnoticed.
    Never auto-update the manifest during a probe. Source changes need review.
    """
    root = Path(root)
    baseline = json.loads(COVERAGE.read_text()) if baseline is None else baseline
    expected = baseline["files"]
    paths = {str(p.relative_to(root)): p for p in (root / "src").rglob("*") if p.is_file()}
    for name in expected:
        if not name.startswith("src/") and (root / name).is_file():
            paths[name] = root / name
    findings = []
    for name in sorted(set(expected) | set(paths)):
        path = paths.get(name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path else None
        if digest == expected.get(name) and name in expected:
            continue
        findings.append({"gate": f"unreviewed-source:{name}", "source": f"{name}:1",
                         "window": "UNKNOWN", "anchor_date": "UNKNOWN",
                         "evidence_timestamp": "UNKNOWN", "expiry_date": "UNKNOWN", "status": "UNKNOWN",
                         "reason": "new_source" if name not in expected else "changed_source" if path else "missing_source",
                         "observed_sha256": digest, "reviewed_sha256": expected.get(name)})
    return findings


def report(root, now, authority_root=None):
    findings = inventory(root, now, authority_root) if authority_root else inventory(root, now)
    coverage = source_coverage(root)
    findings.extend(coverage)
    drifted = {row["source"].rsplit(":", 1)[0] for row in coverage}
    for row in findings:
        row["anchor_date"] = row.get("evidence_timestamp", "UNKNOWN")
        row.setdefault("window", {"days": row.get("window_days"), "hours": row.get("window_hours")})
        event = row["gate"] in ("css-bundle", "public-css", "rendered-freshness-stamp")
        row["expiry_kind"] = "event_driven_no_calendar_deadline" if event else "calendar"
        if event:
            row["expiry_date"] = "UNKNOWN"
        if row["source"].rsplit(":", 1)[0] in drifted:
            row["status"] = "UNKNOWN"
            row["reason"] = "source_adapter_requires_review"
    findings.sort(key=lambda f: (f.get("hours_left", float("inf")), f["gate"]))
    expired = [f for f in findings if f["status"] in ("EXPIRED", "STALE")]
    unknown = [f for f in findings if f["status"] == "UNKNOWN"]
    soon = [f for f in findings if f.get("evaluation_clock") == "wall_clock"
            and 0 <= f.get("hours_left", float("inf")) <= 72]
    return {"schema": "candidate-freshness-horizon-v3", "candidate": str(Path(root).resolve()),
            "observed_at": now.isoformat(), "verdict": "STALE" if expired else "UNKNOWN" if unknown else "CURRENT",
            "coverage": {"reviewed_src_files": sum(p.startswith("src/") for p in json.loads(COVERAGE.read_text())["files"]),
                         "unreviewed_sources": len(coverage), "policy": "any source addition/deletion/change requires adapter review"},
            "gates": len(findings), "expired": expired, "unknown": unknown, "due_within_72h": soon, "all": findings}


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=CAND)
    parser.add_argument("--now", help="ISO timestamp; default actual UTC clock")
    parser.add_argument("--authority-root", type=Path,
                        help="explicit read-only business root containing accepted-evidence loader; absent inputs stay UNKNOWN")
    args = parser.parse_args(argv)
    now = moment(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        parser.error("--now must be an ISO date or timestamp")
    try:
        result = report(args.candidate, now, args.authority_root)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        result = {"schema": "candidate-freshness-horizon-v3", "verdict": "UNKNOWN",
                  "candidate": str(args.candidate), "observed_at": now.isoformat(),
                  "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "CURRENT" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
