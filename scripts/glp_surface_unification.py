#!/usr/bin/env python3
"""Surface unification for glpclinicscout.com - the wire-in machine's LAST STEP.

WHY THIS EXISTS
---------------
Wiring a provider used to mean: research it, give it a review page, add it to the
registry, ship. Everything past the review page was a separate, optional,
usually-skipped step. That produced a two-tier site - 13 "money-class" providers
on the comparison page, the calculator and the quiz, and 16 live providers that
existed only as reviews. The page copy said "13 providers" while 29 were live.

This script closes the loop. It is idempotent and runs against a release
directory. For EVERY live provider (registry: commercial_state=live AND
vertical_fit AND wired AND surfaces.review) it guarantees:

  * a program-content-ledger entry (seeded by affiliate_merge_deep_research.py)
  * a comparison row on the ranked money pages, built only from that provider's
    own dated evidence - never a template sentence, never a borrowed fact
  * a quiz candidate
  * a calculator row where a genuinely comparable published monthly figure
    exists, and a WRITTEN per-provider exception where it does not
  * registry surface flags that match what actually shipped

A provider may be absent from a surface only through an explicit, written
exception in data/program-registry.json. There is no class, tier, or source_kind
that exempts anyone. scripts/glp_deploy_completeness_sensor.py enforces that.

Provider counts in copy are NOT written here. They are emitted as the
{{LIVE_PROVIDER_COUNT}} token and substituted at build time from the derived
live set, so the number can never drift from reality again.

    python3 scripts/glp_surface_unification.py --release DIR [--receipt OUT]
    python3 scripts/glp_surface_unification.py --release CANDIDATE --candidate-in-place

Exit 0 = applied, 1 = failed a data precondition.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
PROFILE = "glp-clinic-scout"
COUNT_TOKEN = "{{LIVE_PROVIDER_COUNT}}"
RANKED_PAGE_IDS = ("/best/online-glp-1-providers/", "/cost/online-glp-1-programs/",
                   "/best/expanded-curated-glp-1-programs/")

# Calculator adjudication is registry-owned. A versioned dynamic profile carries
# either exact comparable first+recurring values or an explicit blank manual mode.
# No provider-specific amount or exception may live in this executable.
DYNAMIC_PROFILE_SCHEMA = "glp-provider-dynamic-profiles-v1"
TRANSACTION_OUTPUTS = (
    "data/program-registry.json", "data/money-page-specs.json", "data/quiz-config.json",
    "data/program-page-eligibility.json", "data/affiliate-activation-selection.json",
    "data/cost-calculator.json",
)

# Fast-path/ready6 rows carry their affiliate link on a provider-issued host that
# is not one of the two network hosts. The host is copied from the provider's own
# contract, never guessed, and is asserted against the link at build time.
NETWORK_HOSTS = {"Katalys": "track.revoffers.com", "Awin": "www.awin1.com"}


def fail(message: str) -> None:
    raise SystemExit(f"surface_unification_failed: {message}")


def load(path: Path):
    return json.loads(path.read_text())


def _https_url(value) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def _nonnegative_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def registry_semantic_basis_sha256(registry: dict) -> str:
    """Match src/therapeutic-taxonomy.mjs stableSha256 for price-index lineage."""
    basis = copy.deepcopy(registry)
    basis.pop("therapeutic_taxonomy", None)
    basis.pop("provider_dynamic_profiles", None)
    basis.pop("consumer_therapeutic_authorities", None)
    canonical = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def latest_release() -> Path:
    dirs = [p for p in RELEASES.iterdir() if (p / "data/program-registry.json").is_file()]
    return max(dirs, key=lambda p: p.stat().st_mtime)


def build_eligibility_entry(program: dict, template: dict, tracking: dict, receipt_sha: str) -> dict:
    pid = render_id(program)
    held = tracking.get('held') is True
    entry = copy.deepcopy(template)
    entry.update({
        'program_id': pid, 'display_name': program['display_name'],
        'review_path': program.get('review_path') or f'/reviews/{pid}/',
        'affiliate_network': tracking['network'],
        'network': str(tracking['network']).lower().split()[0],
        'allowed_fallback_hosts': [] if held else [re.sub(r'^www\.', '', tracking['host'])],
        'affiliate_link_mode': 'held_editorial_only' if held else 'transparent_direct',
        'go_route_wired': False, 'live_link_allowed': not held,
        'promotion_authorized': not held, 'redirector_tos_verified': False,
        'operator_selection_receipt_sha256': None if held else receipt_sha,
        'tracking_present_private': not held,
    })
    if held:
        entry['status'] = 'held_editorial_only'
    return entry


def eligibility_with_retained_holds(rows: list[dict], live_ids: list[str]) -> list[dict]:
    """Keep explicit denial evidence; membership counts must not erase holds."""
    live = set(live_ids)
    return [row for row in rows if row["program_id"] in live or (
        row.get("live_link_allowed") is False
        and row.get("promotion_authorized") is False
        and isinstance(row.get("research_hold_receipt"), dict)
    )]


def raw_direct_live_count(live: list[dict], ledger_by_id: dict) -> int:
    """Raw row count, before relationship denials; match the native strict booleans."""
    return sum(
        1 for program in live
        if (ledger_by_id.get(render_id(program)) or {}).get("live_link_allowed") is True
        or (ledger_by_id.get(render_id(program)) or {}).get("promotion_authorized") is True
    )


def profile_of(program: dict) -> dict:
    return (program.get("profiles") or {}).get(PROFILE) or {}


def live_programs(registry: dict) -> list[dict]:
    """The one definition of 'live provider'. Everything counts from here."""
    out = []
    for program in registry.get("programs") or []:
        profile = profile_of(program)
        if (program.get("commercial_state") == "live"
                and profile.get("vertical_fit") is True
                and profile.get("wired") is True
                and (profile.get("surfaces") or {}).get("review") is True):
            out.append(program)
    return out


def render_id(program: dict) -> str:
    """Ledger/route identity (coivas), not the network registry id (awin-128761)."""
    return str(program.get("review_slug") or program["program_id"])


def dynamic_profiles(registry: dict, live: list[dict]) -> dict[str, dict]:
    """Fail closed unless every canonical live identity has one governed profile."""
    document = registry.get("provider_dynamic_profiles") or {}
    if document.get("schema") != DYNAMIC_PROFILE_SCHEMA or document.get("version") != 1:
        fail("provider_dynamic_profiles_missing")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        fail("provider_dynamic_profile_map_invalid")
    canonical_ids = set()
    alias_ids = set()
    for program in live:
        pid = render_id(program)
        identity = (profile_of(program).get("canonical_identity") or {})
        if identity.get("role") == "alias":
            alias_ids.add(pid)
        else:
            canonical_ids.add(pid)
    if set(profiles) != canonical_ids:
        fail("provider_dynamic_profile_membership_mismatch")
    freshness = document.get("freshness_max_days")
    if not isinstance(freshness, int) or freshness < 1 or freshness > 90:
        fail("provider_dynamic_profile_freshness_invalid")
    today = dt.datetime.now(dt.timezone.utc).date()
    allowed = {
        "molecule_tags": {"semaglutide", "tirzepatide", "retatrutide"},
        "brand_name_tags": {"mounjaro", "wegovy", "zepbound"},
        "delivery_form_tags": {"injectable", "oral"},
        "treatment_type_tags": {"brand_name", "compounded"},
    }
    required_fields = {
        "aliases", "calculator.checked_as_of", "calculator.evidence_gap_copy",
        "calculator.first_month_base_usd", "calculator.mode", "calculator.one_time_fee_usd",
        "calculator.price_context", "calculator.recurring_month_base_usd", "calculator.source_url",
        "program_id", "provider", "registry_program_ids", "source_urls",
        "taxonomy.brand_name_tags", "taxonomy.delivery_form_tags", "taxonomy.molecule_tags",
        "taxonomy.treatment_type_state", "taxonomy.treatment_type_tags", "verified_as_of",
    }
    for pid, profile in profiles.items():
        calculator = profile.get("calculator") or {}
        mode = calculator.get("mode")
        first = calculator.get("first_month_base_usd")
        recurring = calculator.get("recurring_month_base_usd")
        fee = calculator.get("one_time_fee_usd")
        if profile.get("program_id") != pid or profile.get("version") != 1:
            fail(f"provider_dynamic_profile_identity_invalid:{pid}")
        verified = str(profile.get("verified_as_of") or "")
        try:
            profile_age = (today - dt.date.fromisoformat(verified)).days
        except ValueError:
            fail(f"provider_dynamic_profile_date_invalid:{pid}")
        if profile_age < -1 or profile_age > freshness:
            fail(f"provider_dynamic_profile_stale:{pid}")
        source_urls = profile.get("source_urls")
        if (not isinstance(source_urls, list) or not source_urls or source_urls != sorted(set(source_urls))
                or any(not _https_url(url) for url in source_urls)):
            fail(f"provider_dynamic_profile_sources_invalid:{pid}")
        taxonomy = profile.get("taxonomy") or {}
        for field, vocabulary in allowed.items():
            values = taxonomy.get(field)
            if (not isinstance(values, list) or values != sorted(set(values))
                    or any(value not in vocabulary for value in values)):
                fail(f"provider_dynamic_profile_taxonomy_invalid:{pid}:{field}")
        expected_state = "verified" if taxonomy.get("treatment_type_tags") else "not_verified"
        if taxonomy.get("treatment_type_state") != expected_state:
            fail(f"provider_dynamic_profile_taxonomy_state_invalid:{pid}")
        if mode == "autofill":
            if (not _nonnegative_number(first) or not _nonnegative_number(recurring)
                    or not _nonnegative_number(fee) or str(calculator.get("evidence_gap_copy") or "").strip()):
                fail(f"provider_dynamic_profile_autofill_invalid:{pid}")
        elif mode == "manual":
            if first is not None or recurring is not None or fee is not None or len(str(calculator.get("evidence_gap_copy") or "")) < 30:
                fail(f"provider_dynamic_profile_manual_invalid:{pid}")
        else:
            fail(f"provider_dynamic_profile_mode_invalid:{pid}")
        if (len(str(calculator.get("price_context") or "")) < 20
                or calculator.get("source_url") not in source_urls):
            fail(f"provider_dynamic_profile_calculator_evidence_invalid:{pid}")
        bound: dict[str, str] = {}
        bindings = profile.get("evidence_bindings")
        if not isinstance(bindings, list) or not bindings:
            fail(f"provider_dynamic_profile_evidence_missing:{pid}")
        for index, binding in enumerate(bindings):
            fields = binding.get("fields") if isinstance(binding, dict) else None
            text = binding.get("evidence_text") if isinstance(binding, dict) else None
            digest = str(binding.get("evidence_sha256") or "") if isinstance(binding, dict) else ""
            if (not isinstance(fields, list) or not fields or fields != sorted(set(fields))
                    or any(field not in required_fields for field in fields)):
                fail(f"provider_dynamic_profile_evidence_fields_invalid:{pid}:{index}")
            if not isinstance(text, str) or not text or digest != hashlib.sha256(text.encode()).hexdigest():
                fail(f"provider_dynamic_profile_evidence_hash_invalid:{pid}:{index}")
            if binding.get("source_url") not in source_urls or not _https_url(binding.get("source_url")):
                fail(f"provider_dynamic_profile_evidence_source_invalid:{pid}:{index}")
            for field in fields:
                if field in bound and bound[field] != digest:
                    fail(f"provider_dynamic_profile_evidence_conflict:{pid}:{field}")
                bound[field] = digest
        missing = sorted(required_fields - set(bound))
        if missing:
            fail(f"provider_dynamic_profile_evidence_coverage:{pid}:{','.join(missing)}")
    return profiles


def first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind(" ")] + "..."


ACTIVE_INSURANCE = re.compile(
    r"insurance (?:check|support|verification|navigation|accepted|billing)|prior[- ]auth|carrier"
    r"|coverage check|bills? (?:your )?insurance|in[- ]network|through insurance|insurance copay", re.I)
CASH_ONLY = re.compile(
    r"cash[- ]pay only|does not (?:bill|accept) (?:health )?insurance|insurance is not (?:used|accepted)", re.I)
ONGOING_SUPPORT = re.compile(r"coach|check[- ]in|ongoing (?:provider )?support|care team|dietitian|follow[- ]up", re.I)
PUBLIC_PAYER = re.compile(r"medicare|medicaid", re.I)
FORMS = (re.compile(r"\binject", re.I), re.compile(r"\boral\b|\btablet\b|\bpill\b", re.I),
         re.compile(r"brand[- ]name|branded|wegovy|zepbound|ozempic|mounjaro", re.I),
         re.compile(r"compounded", re.I))


def sourced_value(entry: dict, field: str) -> str:
    node = entry.get(f"research_{field}")
    if not isinstance(node, dict) or node.get("state") == "not_publicly_stated":
        return ""
    value = node.get("value")
    return value if isinstance(value, str) and value.strip() else ""


def evidence_flags(entry: dict, pid: str, governed_profile: dict) -> dict:
    """What this provider actually publishes, with price from governed adjudication."""
    meds_value = entry.get("meds_offered")
    # Deep-research rows may use a string list; consume its text, not repr/list.
    if isinstance(meds_value, list):
        if not all(isinstance(value, str) for value in meds_value):
            fail(f"meds_offered_type_invalid:{pid}")
        meds = " ".join(meds_value)
    elif meds_value is None or isinstance(meds_value, str):
        meds = meds_value or ""
    else:
        fail(f"meds_offered_type_invalid:{pid}")
    blob = " ".join(filter(None, [
        entry.get("insurance"), meds, entry.get("starting_price"),
        sourced_value(entry, "consultation_fee"), sourced_value(entry, "first_month_price"),
    ]))
    return {
        "comparable_price": (governed_profile.get("calculator") or {}).get("mode") == "autofill",
        "insurance_support": bool(ACTIVE_INSURANCE.search(blob)) and not bool(CASH_ONLY.search(entry.get("insurance") or "")),
        "ongoing_support": bool(ONGOING_SUPPORT.search(blob)),
        "multi_form": sum(1 for pattern in FORMS if pattern.search(meds)) >= 2,
        "public_payer": bool(PUBLIC_PAYER.search(blob)),
        "shipping": bool(sourced_value(entry, "shipping_wait")),
        "states": bool(sourced_value(entry, "states_served")),
        "cancellation": bool(sourced_value(entry, "cancellation")),
        "pharmacy": bool(sourced_value(entry, "pharmacy_type")),
    }


def why_match(display: str, flags: dict, entry: dict) -> str:
    publishes = [label for key, label in (
        ("comparable_price", "a comparable monthly price"),
        ("insurance_support", "active insurance support"),
        ("ongoing_support", "ongoing clinical support"),
        ("multi_form", "more than one medication format"),
        ("pharmacy", "its dispensing pharmacy chain"),
        ("shipping", "a shipping timeframe"),
        ("cancellation", "cancellation terms"),
        ("states", "the states it serves"),
    ) if flags.get(key)]
    if not publishes:
        return (f"{display} is matched only on the answers you gave; its published commercial evidence is thin, "
                "so confirm price, cadence and terms directly before paying.")
    listed = ", ".join(publishes[:-1]) + (" and " + publishes[-1] if len(publishes) > 1 else publishes[0] if len(publishes) == 1 else "")
    return (f"{display} publishes {listed}. This is a commercial fit on your stated priorities, not a clinical "
            "recommendation; verify current terms at the source.")


# --------------------------------------------------------------------------
# Row construction. Every field traces to one artifact.
# --------------------------------------------------------------------------
def logo_for(program_id: str, logo_manifest: dict) -> dict | None:
    for logo in logo_manifest.get("logos") or []:
        if logo.get("program_id") == program_id:
            return {
                "alt": f"{logo['provider']} logo",
                "height": logo["height"],
                "media_type": logo["media_type"],
                "path": logo["public_path"],
                "runtime_remote_load": False,
                "sha256": logo["output_sha256"],
                "source_asset_url": logo.get("source_asset_url"),
                "source_page_url": logo.get("resolved_url"),
                "surface": logo.get("surface") or "light",
                "verified_as_of": logo["verified_as_of"],
                "width": logo["width"],
            }
    return None


def build_row(program: dict, ledger_entry: dict, contract: dict | None,
              ready6: dict | None, tracking: dict, logo_manifest: dict,
              receipt_sha: str, checked_as_of: str) -> dict:
    pid = render_id(program)
    display = ledger_entry.get("display_name") or program["display_name"]
    row_contract = (contract or {}).get("row_contract") or {}
    page = (ready6 or {}).get("page") or {}

    if row_contract:
        pros = [p for p in (row_contract.get("pros") or []) if p][:5]
        cons = [c for c in (row_contract.get("cons") or []) if c][:5]
        bottom_line = ((contract or {}).get("page_copy") or {}).get("bottom_line") or ""
        best_for = row_contract.get("best_for") or ""
        meds = row_contract.get("medications") or ledger_entry.get("meds_offered") or ""
        insurance = row_contract.get("insurance") or ledger_entry.get("insurance") or ""
    elif page:
        pros = [p for p in (page.get("first_party_findings") or []) if p][:5]
        cons = [c for c in (page.get("evidence_gaps") or []) if c][:5]
        bottom_line = page.get("bottom_line") or ""
        best_for = "Readers who want the published terms checked before paying"
        meds = ledger_entry.get("meds_offered") or ""
        insurance = ledger_entry.get("insurance") or ""
    else:
        fail(f"no_row_source:{pid}")

    # Do not pad an accepted research contract with a second invented benefit.
    # At least one evidenced pro and two explicit limitations remain required.
    if len(pros) < 1 or len(cons) < 2:
        fail(f"row_evidence_too_thin:{pid}")

    href = tracking["url"]
    host = tracking["host"]
    row = {
        "affiliate_href": href,
        "affiliate_link_mode": "transparent_direct",
        "affiliate_network": tracking["network"],
        "affiliate_tracking_host": host,
        "average_wait": ledger_entry.get("average_wait") or "Not stated",
        "best_for": best_for,
        "bottom_line": bottom_line or f"{display}: see the dated evidence and open questions below before enrolling.",
        "cons": cons,
        "criteria_scores": ledger_entry.get("criteria_scores") or {},
        "insurance": insurance or "Not stated",
        "live_link_allowed": tracking.get('held') is not True,
        "meds_offered": meds or "Prescription and clinician approval required",
        "operator_selection_receipt_sha256": receipt_sha,
        "program_id": pid,
        "promo": {
            "code": None,
            "label": "No verified promo code",
            "source_url": None,
            "status": "NOT_VERIFIED",
            "verified_as_of": checked_as_of,
        },
        "promotion_authorized": tracking.get('held') is not True,
        "pros": pros,
        "provider": display,
        "review_path": program["review_path"],
        "score": None,
        "social_proof": {
            "explanation": "No comparable independent public-review dataset was verified for this comparison.",
            "label": "Independent public reviews",
            "methodology_path": "/how-we-rate/",
            "status": "NOT_VERIFIED_COMPARABLE",
            "verified_as_of": checked_as_of,
        },
        "source_url": ledger_entry.get("source_url"),
        "starting_price": ledger_entry.get("starting_price"),
        # MISDIAGNOSIS 2026-09-02, recorded so it is not repeated: this line was
        # briefly forced to None while chasing
        # cost_calculator_fastpath_row_invalid:altrx-glp1-us-cps. That was the
        # wrong artifact. build_row() feeds data/money-page-specs.json - the
        # COMPARISON row, where a sortable monthly figure is exactly what is
        # wanted and nulling it silently degrades price sorting. This module
        # never writes data/provider-page-contracts.json at all (it only reads
        # it). The cost-calculator invariant applies to the CONTRACT row, and it
        # is enforced where contract rows are written - see
        # _contract_row_from_spec in glp_affiliate_wire_backlog_worker.py.
        "starting_price_sort": ledger_entry.get("starting_price_sort"),
        "wire_in_lane": program.get("source_kind"),
    }
    logo = logo_for(pid, logo_manifest)
    if logo:
        row["logo"] = logo
    if not row["source_url"] or not str(row["source_url"]).startswith("https://"):
        fail(f"row_source_url_missing:{pid}")
    if not row["starting_price"]:
        fail(f"row_starting_price_missing:{pid}")
    return row


def tracking_for(program: dict, contract: dict | None, approved: dict) -> dict:
    pid = program["program_id"]
    if contract:
        network_contract = contract.get("network_contract") or {}
        url = network_contract.get("tracking_url")
        network = network_contract.get("network") or "Direct affiliate"
        if not url:
            row = contract.get('row_contract') or {}
            if ((program.get('affiliate_relationship_snapshot') or {}).get('promotion_denied') is True
                    and network_contract.get('promotion_authorized_in_this_artifact') is False
                    and row.get('live_link_allowed') is False and row.get('promotion_authorized') is False):
                return {'url': None, 'host': None, 'network': network, 'held': True}
            fail(f"tracking_url_missing:{pid}")
        host = re.sub(r"^https://([^/]+).*$", r"\1", url)
        return {"url": url, "host": host, "network": network}
    for entry in approved.get("programs") or []:
        if entry.get("registry_id") == pid:
            tracking = entry.get("tracking") or {}
            url = tracking.get("provider_issued_url")
            host = tracking.get("tracking_host")
            network = entry.get("network")
            if not url or host != NETWORK_HOSTS.get(network):
                fail(f"tracking_host_not_allowlisted:{pid}")
            return {"url": url, "host": host, "network": network}
    fail(f"tracking_authority_missing:{pid}")


# --------------------------------------------------------------------------
def _unify_in_place(release: Path) -> dict:
    data = release / "data"
    registry = load(data / "program-registry.json")
    ledger = load(data / "program-content-ledger.json")
    specs = load(data / "money-page-specs.json")
    quiz = load(data / "quiz-config.json")
    eligibility = load(data / "program-page-eligibility.json")
    activation = load(data / "affiliate-activation-selection.json")
    contracts_doc = load(data / "provider-page-contracts.json")
    fastpath = load(data / "provider-fastpath-contract.json")
    ready6_doc = load(data / "ready6-editorial-contract.json")
    approved = load(data / "approved-program-registry.json")
    calculator = load(data / "cost-calculator.json")
    logo_manifest = load(release / "public/assets/provider-logos/manifest.json")

    live = live_programs(registry)
    live_ids = [render_id(p) for p in live]
    live_count = sum(1 for program in live
                     if (profile_of(program).get("canonical_identity") or {}).get("role") != "alias")
    governed_profiles = dynamic_profiles(registry, live) if isinstance(registry.get("therapeutic_taxonomy"), dict) else {}
    receipt_sha = activation["operator_gate_receipt_sha256"]
    checked_as_of = fastpath.get("providers", [{}])[0].get("checked_as_of") or dt.date.today().isoformat()

    ledger_by_id = {str(e["program_id"]): e for e in ledger["programs"]}
    contracts = {c["program_id"]: c for c in contracts_doc["providers"]}
    ready6 = {p["registry_id"]: p for p in ready6_doc["programs"]}

    missing_ledger = [pid for pid in live_ids if pid not in ledger_by_id]
    if missing_ledger:
        fail("ledger_incomplete_run_affiliate_merge_deep_research_first:" + ",".join(missing_ledger))

    receipt = {"live_provider_count": live_count, "live_provider_ids": sorted(live_ids),
               "rows_added": [], "quiz_added": [], "calculator_added": [],
               "exceptions_voided": [], "exceptions_kept": [], "count_tokens": []}

    # ---- 1. comparison rows on every ranked page ---------------------------
    for page in specs["pages"]:
        if page["path"] not in RANKED_PAGE_IDS:
            continue
        present = {row["program_id"] for row in page["rows"]}
        sources_by_url = {s["url"]: s for s in page["sources"]}
        for program in live:
            pid = render_id(program)
            if pid in present:
                continue
            entry = ledger_by_id[pid]
            contract = contracts.get(program["program_id"])
            tracking = tracking_for(program, contract, approved)
            row = build_row(program, entry, contract, ready6.get(program["program_id"]),
                            tracking, logo_manifest, receipt_sha, checked_as_of)
            source_url = row["source_url"]
            if source_url not in sources_by_url:
                source = {"publisher": row["provider"], "source_id": f"source-{len(page['sources']) + 1}",
                          "url": source_url,
                          "verified_as_of": (entry.get("research_first_month_price") or {}).get("verified_as_of") or checked_as_of}
                page["sources"].append(source)
                sources_by_url[source_url] = source
            row["citation_source_id"] = sources_by_url[source_url]["source_id"]
            page["rows"].append(row)
            receipt["rows_added"].append({"page": page["path"], "program_id": pid})
        # source ids must stay 1..N in order - the renderer asserts it
        for index, source in enumerate(page["sources"]):
            new_id = f"source-{index + 1}"
            if source["source_id"] != new_id:
                for row in page["rows"]:
                    if row.get("citation_source_id") == source["source_id"]:
                        row["citation_source_id"] = new_id
                source["source_id"] = new_id

    # ---- 2. quiz candidates ------------------------------------------------
    # Matching rules are DERIVED from what the provider publishes, never assigned
    # by class. A provider appears under "price" only if it publishes a comparable
    # monthly figure, under "insurance support" only if it publishes active
    # insurance help, and so on. An answer a provider's evidence cannot speak to
    # excludes it rather than matching it on a guess.
    quiz_present = {c["program_id"] for c in quiz["candidates"]}
    for program in live:
        pid = render_id(program)
        if pid in quiz_present:
            continue
        entry = ledger_by_id[pid]
        display = entry.get("display_name") or program["display_name"]
        governed = governed_profiles.get(pid)
        if governed is None:
            fail(f"provider_dynamic_profile_missing:{pid}")
        published = evidence_flags(entry, pid, governed)
        goal = ["prefer-not-to-say"]
        if published["comparable_price"]:
            goal.append("price")
        if published["insurance_support"]:
            goal.append("insurance-support")
        if published["ongoing_support"]:
            goal.append("coaching")
        if published["multi_form"]:
            goal.append("medication-form")
        pay = ["unsure", "prefer-not-to-say", "self-pay"]
        if published["insurance_support"]:
            pay.append("commercial-insurance")
        if published["public_payer"]:
            pay.append("medicare-medicaid")
        candidate = {
            "display_name": display,
            "held_route": f"{(program.get('go') or {}).get('route')}?s=provider-fit.result&v=a",
            "live_link_allowed": False,
            "priority_score": 0,  # replaced at build with the merit score
            "program_id": pid,
            "promotion_authorized": False,
            "rules": {"goal": sorted(set(goal)), "insurance": sorted(set(pay))},
            "source_clearance": True,
            "staging_eligible": True,
            "why_match": why_match(display, published, entry),
        }
        quiz["candidates"].append(candidate)
        quiz_present.add(pid)
        receipt["quiz_added"].append({"program_id": pid, "rules": candidate["rules"]})
    quiz["candidate_derivation"] = (
        "Candidates and matching rules are derived by scripts/glp_surface_unification.py from each provider's "
        "own published evidence in data/program-content-ledger.json. priority_score is the merit score computed "
        "at build by src/merit-ranking.mjs, so the quiz and the comparison page cannot disagree on rank."
    )

    # ---- 3. calculator -----------------------------------------------------
    calculator["first_month_overrides"] = {}
    calculator["recurring_overrides"] = {}
    calculator["one_time_overrides"] = {}
    calculator["dynamic_profile_schema"] = DYNAMIC_PROFILE_SCHEMA
    calculator["override_basis"] = (
        "No hardcoded override is final authority. program-registry provider_dynamic_profiles "
        "carries evidence-derived autofill or blank manual-entry adjudication for every canonical provider."
    )
    for pid, governed in sorted(governed_profiles.items()):
        adjudication = governed["calculator"]
        receipt["calculator_added"].append({
            "program_id": pid,
            "mode": adjudication["mode"],
            "first_month_usd": adjudication.get("first_month_base_usd"),
            "recurring_usd": adjudication.get("recurring_month_base_usd"),
            "basis": adjudication.get("price_context"),
        })

    # ---- 4. registry surfaces + written exceptions --------------------------
    for program in registry["programs"]:
        profile = profile_of(program)
        if not profile:
            continue
        pid = render_id(program)
        if pid not in live_ids:
            continue
        identity = profile.get("canonical_identity") or {}
        is_alias = identity.get("role") == "alias"
        surfaces = profile.setdefault("surfaces", {})
        exceptions = profile.setdefault("exceptions", {})
        if is_alias:
            reason = str(identity.get("reason") or "")
            if len(reason) < 40:
                fail(f"canonical_alias_reason_missing:{pid}")
            for surface in ("compare", "quiz", "calculator"):
                surfaces[surface] = False
                exceptions[surface] = {
                    "enabled": True,
                    "reason": reason,
                    "surface_restriction_basis": "canonical_identity_alias",
                }
        else:
            for surface in ("compare", "quiz", "calculator"):
                exception = exceptions.get(surface) or {}
                documented_restriction = (
                    surface in {"quiz", "calculator"}
                    and exception.get("enabled") is True
                    and bool(str(exception.get("surface_restriction_basis") or "").strip())
                )
                if documented_restriction:
                    surfaces[surface] = False
                    receipt["exceptions_kept"].append({
                        "program_id": pid,
                        "surface": surface,
                        "reason": exception.get("reason"),
                        "surface_restriction_basis": exception["surface_restriction_basis"],
                    })
                    continue
                surfaces[surface] = True
                if surface in exceptions:
                    receipt["exceptions_voided"].append({
                        "program_id": pid, "surface": surface,
                        "voided_reason": exceptions.pop(surface).get("reason"),
                        "why_voided": "The exception has no governed surface_restriction_basis. "
                                      "src/merit-ranking.mjs derives list-surface evidence from this provider's "
                                      "own dated research fields, so an undocumented gap cannot stay active.",
                    })
        if not is_alias and pid not in governed_profiles:
            fail(f"provider_dynamic_profile_missing:{pid}")
        if "go" in exceptions and surfaces.get("go") is not True:
            receipt["exceptions_kept"].append({"program_id": pid, "surface": "go",
                                              "reason": exceptions["go"].get("reason")})

    # ---- 5. side files that gate the live count -----------------------------
    eligibility_ids = {p["program_id"] for p in eligibility["programs"]}
    elig_template = eligibility["programs"][0]
    for program in live:
        pid = render_id(program)
        if pid in eligibility_ids:
            continue
        contract = contracts.get(program["program_id"])
        tracking = tracking_for(program, contract, approved)
        entry = build_eligibility_entry(program, elig_template, tracking, receipt_sha)
        eligibility["programs"].append(entry)
        eligibility_ids.add(pid)
    eligibility["programs"] = eligibility_with_retained_holds(eligibility["programs"], live_ids)
    # NOT live_count. live_count is MERCHANT identities (aliases collapsed) and is
    # what reader-facing copy says. These two fields are read by build-staging.mjs
    # as the direct-affiliate ROW count - one row per affiliate relationship,
    # aliases included, link-dark programs excluded. The two numbers happened to
    # be equal (33) for as long as the site had exactly one alias row and exactly
    # one link-dark program, so this wrote the wrong quantity for weeks without
    # anyone noticing. Lifting TRT Nation on 2026-08-18 separated them (33 vs 34)
    # and build-staging's affiliate_activation_live_count_desynced guard caught it.
    direct_live_row_count = raw_direct_live_count(live, ledger_by_id)
    eligibility["live_program_count"] = direct_live_row_count
    eligibility["program_count"] = len(eligibility["programs"])
    eligibility["derivation"] = (
        "Regenerated by scripts/glp_surface_unification.py from data/program-registry.json: every program with "
        "commercial_state=live, vertical_fit=true, wired=true and surfaces.review=true, keyed by render identity. "
        "One tier: no source_kind, class, or lane changes membership."
    )
    activation["live_program_count"] = direct_live_row_count
    activation["live_program_count_basis"] = (
        "Derived from data/program-registry.json (commercial_state=live AND vertical_fit AND wired AND "
        "surfaces.review), keyed by render identity and intersected with data/program-content-ledger.json. "
        "program_count stays the Katalys Sheet direct-activation subset with tracking-link hashes; it is NOT "
        "the count of live providers on the site and must never be rendered as one."
    )

    # ---- 6. counts in copy become build-time tokens --------------------------
    # Any spelled or numeric provider count in copy becomes the build-time token.
    # The lookbehind keeps "GLP-1 programs" from reading as "1 programs" - the
    # count sits in prose, and prose about a drug class is not a count.
    number_words = {"seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
                    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
                    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
                    "twenty-four": 24, "twenty-five": 25, "twenty-six": 26,
                    "twenty-seven": 27, "twenty-eight": 28, "twenty-nine": 29, "thirty": 30}
    QUALIFIER = r"(?:additional\s+|more\s+|other\s+|curated\s+|online\s+|GLP[-\u2011]?1\s+)*"
    NOUN = r"(providers|programs|clinics)"
    numeric = re.compile(rf"(?<![-\u2011\d]){{0}}(\d{{{{1,3}}}})\s+{QUALIFIER}{NOUN}\b".format(""), re.I)
    worded = re.compile(rf"\b({'|'.join(sorted(number_words, key=len, reverse=True))})\s+{QUALIFIER}{NOUN}\b", re.I)

    def tokenize(text: str, where: str) -> str:
        def replace(match: re.Match) -> str:
            receipt["count_tokens"].append({"where": where, "was": match.group(0)})
            middle = match.group(0)[match.end(1) - match.start(0):match.start(2) - match.start(0)]
            # "Seven additional programs" described a subset page that now carries
            # the whole set. Keep the derived count and drop the subset word rather
            # than shipping "29 additional programs", which would be a new lie.
            middle = re.sub(r"\b(?:additional|more|other)\s+", "", middle)
            return f"{COUNT_TOKEN}{middle}{match.group(2)}"

        return worded.sub(replace, numeric.sub(replace, text))

    def walk(node, where: str):
        if isinstance(node, str):
            return tokenize(node, where)
        if isinstance(node, list):
            return [walk(item, where) for item in node]
        if isinstance(node, dict):
            return {key: (value if key in ("url", "source_url", "affiliate_href", "path", "review_path",
                                           "program_id", "citation_source_id", "source_id")
                          else walk(value, f"{where}.{key}"))
                    for key, value in node.items()}
        return node

    for page in specs["pages"]:
        for field in ("intro", "faq", "verdict", "description", "cons", "pros", "h1", "title",
                      "best_for", "decision_matrix", "section_winners"):
            if field in page:
                page[field] = walk(page[field], f"{page['path']}.{field}")

    dump(data / "program-registry.json", registry)
    dump(data / "money-page-specs.json", specs)
    dump(data / "quiz-config.json", quiz)
    dump(data / "program-page-eligibility.json", eligibility)
    dump(data / "affiliate-activation-selection.json", activation)
    dump(data / "cost-calculator.json", calculator)

    # Build 6+: taxonomy is registry-owned and both compare + quiz consume it at
    # build time. Reconcile after every wire-in mutation so a new current provider
    # either contributes one normalized evidence object or the wire-in fails before
    # a renderer can guess. Pre-Build-6 releases have no taxonomy and remain on the
    # prior behavior until the governed Build 6 candidate is admitted.
    if isinstance(registry.get("therapeutic_taxonomy"), dict):
        reconcile = release / "scripts/reconcile-therapeutic-wire-in.mjs"
        if not reconcile.is_file():
            fail("therapeutic_reconcile_script_missing")
        proc = subprocess.run(
            ["node", str(reconcile), "--release", str(release)],
            text=True, capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            fail(f"therapeutic_reconcile_failed:{proc.stderr.strip() or proc.stdout.strip()}")
        try:
            receipt["therapeutic_taxonomy"] = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            fail("therapeutic_reconcile_receipt_invalid")

        check = subprocess.run(
            ["node", str(reconcile), "--release", str(release), "--check"],
            text=True, capture_output=True, timeout=120,
        )
        if check.returncode != 0:
            fail(f"therapeutic_reconcile_check_failed:{check.stderr.strip() or check.stdout.strip()}")

        # Registry program bytes changed during this wire-in, while the weekly
        # price-index remains pinned to its frozen source. Refresh only the
        # semantic basis guard that deliberately excludes taxonomy/dynamic nodes;
        # never rewrite the frozen source hash or observations.
        reconciled_registry = load(data / "program-registry.json")
        semantic_sha256 = registry_semantic_basis_sha256(reconciled_registry)
        reconciled_registry["therapeutic_taxonomy"][
            "price_index_registry_programs_semantic_basis_sha256"
        ] = semantic_sha256
        # Preserve the canonical reconciler's object-key order: the release's JS
        # validator compares expected_counts to its derived object byte-for-byte.
        (data / "program-registry.json").write_text(
            json.dumps(reconciled_registry, indent=2) + "\n"
        )
        receipt["price_index_registry_programs_semantic_basis_sha256"] = semantic_sha256

    receipt["schema"] = "glp-surface-unification-receipt-v1"
    receipt["release"] = release.name
    receipt["applied_at_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return receipt


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_snapshot(release: Path) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    for relative in TRANSACTION_OUTPUTS:
        path = release / relative
        if not path.is_file() or path.is_symlink():
            fail(f"transaction_output_invalid:{relative}")
        snapshot[relative] = {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
    return snapshot


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock_path(release: Path) -> Path:
    return release.parent / f".{release.name}.glp-unification.lock"


def _assert_release_lock(release: Path, lock: dict) -> None:
    if lock.get("release") != str(release.resolve()) or lock.get("pid") != os.getpid():
        fail("transaction_lock_owner_mismatch")
    descriptor = lock.get("descriptor")
    if not isinstance(descriptor, int):
        fail("transaction_lock_descriptor_invalid")
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(lock["path"])
    except OSError as exc:
        fail(f"transaction_lock_stat_failed:{exc}")
    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1 or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (opened.st_dev, opened.st_ino) != (lock["device"], lock["inode"])):
        fail("transaction_lock_identity_drift")


@contextlib.contextmanager
def _release_lock(release: Path):
    """One no-follow release-scoped flock for every snapshot/write/recovery path."""
    release = release.resolve()
    lock_path = _lock_path(release)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        fail("transaction_lock_nofollow_unavailable")
    flags |= nofollow
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        fail(f"transaction_lock_open_failed:{exc}")
    lock = None
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(lock_path)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                or opened.st_nlink != 1 or named.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            fail("transaction_lock_not_plain_regular")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        lock = {
            "descriptor": descriptor,
            "path": str(lock_path),
            "release": str(release),
            "pid": os.getpid(),
            "device": opened.st_dev,
            "inode": opened.st_ino,
        }
        _assert_release_lock(release, lock)
        yield lock
    finally:
        if lock is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _write_journal(path: Path, body: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, sort_keys=True) + "\n")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _recover_transactions(release: Path, lock: dict) -> None:
    _assert_release_lock(release, lock)
    prefix = f".{release.name}.glp-unification-txn-"
    for transaction in sorted(release.parent.glob(prefix + "*")):
        if transaction.is_symlink() or not transaction.is_dir():
            fail(f"transaction_recovery_directory_invalid:{transaction.name}")
        journal_path = transaction / "journal.json"
        if journal_path.is_symlink() or not journal_path.is_file():
            shutil.rmtree(transaction, ignore_errors=True)
            continue
        journal = load(journal_path)
        if journal.get("release") != str(release.resolve()):
            fail(f"transaction_recovery_scope_mismatch:{transaction.name}")
        if (journal.get("schema") != "glp-surface-unification-transaction-v2"
                or journal.get("lock_path") != lock["path"]
                or journal.get("transaction_id") != transaction.name.rsplit("-", 1)[-1]
                or journal.get("outputs") != list(TRANSACTION_OUTPUTS)):
            fail(f"transaction_recovery_ownership_invalid:{transaction.name}")
        phase = journal.get("phase")
        expected = journal.get("expected")
        incoming = journal.get("incoming")
        if set(expected or {}) != set(TRANSACTION_OUTPUTS) or set(incoming or {}) != set(TRANSACTION_OUTPUTS):
            fail(f"transaction_recovery_snapshot_invalid:{transaction.name}")
        current = _output_snapshot(release)
        if phase in {"preparing", "prepared"}:
            if journal.get("committed_count") != 0:
                fail(f"transaction_recovery_count_invalid:{transaction.name}")
        elif phase == "committing":
            ownership = {}
            for relative in TRANSACTION_OUTPUTS:
                if current[relative] == incoming[relative]:
                    ownership[relative] = "incoming"
                elif current[relative] == expected[relative]:
                    ownership[relative] = "opening"
                else:
                    fail(f"transaction_recovery_ownership_conflict:{relative}")
            # Preflight every target before the first restoration, so a foreign
            # byte can never be partly masked by stale transaction recovery.
            for relative in TRANSACTION_OUTPUTS:
                if ownership[relative] != "incoming":
                    continue
                backup = transaction / "backup" / relative
                target = release / relative
                if backup.is_symlink() or not backup.is_file() or {
                    "sha256": _sha256_file(backup), "bytes": backup.stat().st_size,
                } != expected[relative]:
                    fail(f"transaction_recovery_backup_missing:{relative}")
                replacement = target.with_name(f".{target.name}.recovery-{uuid.uuid4().hex}")
                shutil.copy2(backup, replacement)
                os.replace(replacement, target)
                _fsync_dir(target.parent)
            if _output_snapshot(release) != expected:
                fail(f"transaction_recovery_hash_mismatch:{transaction.name}")
        elif phase == "committed":
            if current != incoming:
                fail(f"transaction_recovery_committed_drift:{transaction.name}")
        else:
            fail(f"transaction_recovery_phase_invalid:{transaction.name}")
        shutil.rmtree(transaction)
        _fsync_dir(release.parent)


def _transactional_commit_outputs(release: Path, staged: Path, expected: dict[str, dict],
                                  *, fault_after_replace: int | None = None,
                                  before_cas=None, after_committing_journal=None,
                                  hard_crash_after_replace: int | None = None) -> dict[str, dict]:
    """CAS-commit all six files; every exception restores the exact opening bytes."""
    release = release.resolve()
    with _release_lock(release) as lock:
        _recover_transactions(release, lock)
        transaction_id = uuid.uuid4().hex
        transaction = release.parent / f".{release.name}.glp-unification-txn-{transaction_id}"
        transaction.mkdir(mode=0o700)
        commit_started = False
        try:
            for relative in TRANSACTION_OUTPUTS:
                source = staged / relative
                if not source.is_file() or source.is_symlink():
                    fail(f"staged_output_invalid:{relative}")
            incoming_snapshot = _output_snapshot(staged)
            journal = {
                "schema": "glp-surface-unification-transaction-v2",
                "transaction_id": transaction_id,
                "release": str(release),
                "lock_path": lock["path"],
                "owner_pid": os.getpid(),
                "phase": "preparing",
                "committed_count": 0,
                "outputs": list(TRANSACTION_OUTPUTS),
                "expected": expected,
                "incoming": incoming_snapshot,
            }
            for relative in TRANSACTION_OUTPUTS:
                source = staged / relative
                backup = transaction / "backup" / relative
                incoming = transaction / "incoming" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                incoming.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(release / relative, backup)
                shutil.copy2(source, incoming)
                with backup.open("rb") as handle:
                    os.fsync(handle.fileno())
                with incoming.open("rb") as handle:
                    os.fsync(handle.fileno())
            journal["phase"] = "prepared"
            _write_journal(transaction / "journal.json", journal)
            if before_cas:
                before_cas()
            if _output_snapshot(release) != expected:
                fail("transaction_cas_conflict")
            journal["phase"] = "committing"
            _write_journal(transaction / "journal.json", journal)
            if after_committing_journal:
                after_committing_journal()
            # Binding CAS: this is intentionally the final operation before the
            # first replacement and occurs while the one release lock is held.
            if _output_snapshot(release) != expected:
                fail("transaction_cas_conflict")
            commit_started = True
            for index, relative in enumerate(TRANSACTION_OUTPUTS, 1):
                target = release / relative
                incoming = transaction / "incoming" / relative
                if {"sha256": _sha256_file(target), "bytes": target.stat().st_size} != expected[relative]:
                    fail(f"transaction_cas_conflict:{relative}")
                os.replace(incoming, target)
                _fsync_dir(target.parent)
                journal["committed_count"] = index
                _write_journal(transaction / "journal.json", journal)
                if hard_crash_after_replace == index:
                    os._exit(91)
                if fault_after_replace == index:
                    raise RuntimeError(f"injected_commit_fault:{index}")
            journal["phase"] = "committed"
            _write_journal(transaction / "journal.json", journal)
            result = _output_snapshot(release)
            shutil.rmtree(transaction)
            _fsync_dir(release.parent)
            return result
        except BaseException:
            # The lock excludes every compliant writer. Restore only after this
            # owner began replacing, then prove exact opening bytes before unlock.
            if commit_started:
                for relative in TRANSACTION_OUTPUTS:
                    backup = transaction / "backup" / relative
                    target = release / relative
                    if backup.is_file():
                        replacement = target.with_name(f".{target.name}.rollback-{uuid.uuid4().hex}")
                        shutil.copy2(backup, replacement)
                        os.replace(replacement, target)
                        _fsync_dir(target.parent)
            shutil.rmtree(transaction, ignore_errors=True)
            _fsync_dir(release.parent)
            if commit_started and _output_snapshot(release) != expected:
                fail("transaction_rollback_hash_mismatch")
            raise


def _validate_staged_outputs(staged: Path) -> dict[str, dict]:
    snapshot = _output_snapshot(staged)
    for relative in TRANSACTION_OUTPUTS:
        try:
            load(staged / relative)
        except (OSError, ValueError) as exc:
            fail(f"staged_json_invalid:{relative}:{exc}")
    return snapshot


def unify(release: Path, *, fault_after_replace: int | None = None, before_cas=None) -> dict:
    """Stage, reconcile, validate, and CAS-commit with exact rollback semantics."""
    release = release.resolve()
    with _release_lock(release) as lock:
        _recover_transactions(release, lock)
        expected = _output_snapshot(release)
    stage_parent = Path(tempfile.mkdtemp(prefix=f".{release.name}.glp-unification-stage-", dir=release.parent))
    os.chmod(stage_parent, 0o700)
    staged = stage_parent / "candidate"
    try:
        shutil.copytree(release, staged, copy_function=shutil.copy2)
        receipt = _unify_in_place(staged)
        staged_snapshot = _validate_staged_outputs(staged)
        committed = _transactional_commit_outputs(
            release, staged, expected, fault_after_replace=fault_after_replace, before_cas=before_cas,
        )
        if committed != staged_snapshot:
            fail("transaction_commit_hash_mismatch")
        receipt["release"] = release.name
        receipt["transaction"] = {
            "schema": "glp-surface-unification-transaction-v1",
            "staged_private": True,
            "cas_opening": expected,
            "committed": committed,
            "rollback_recovery": True,
            "output_count": len(TRANSACTION_OUTPUTS),
        }
        return receipt
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
        _fsync_dir(release.parent)


def unify_candidate_in_place(release: Path) -> dict:
    """Mutate one caller-owned candidate without staging a full byte-copy tree.

    The caller must detach all ``TRANSACTION_OUTPUTS`` before invoking this mode.
    There is intentionally no transaction copy/commit here: on failure the
    affiliate constructor discards its entire private candidate.
    """
    release = release.resolve()
    opening = _output_snapshot(release)
    receipt = _unify_in_place(release)
    committed = _validate_staged_outputs(release)
    receipt["release"] = release.name
    receipt["transaction"] = {
        "schema": "glp-surface-unification-candidate-in-place-v1",
        "candidate_only": True,
        "full_tree_copy": False,
        "opening": opening,
        "committed": committed,
        "outputs": list(TRANSACTION_OUTPUTS),
        "output_count": len(TRANSACTION_OUTPUTS),
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release")
    parser.add_argument("--receipt")
    parser.add_argument(
        "--candidate-in-place",
        action="store_true",
        help="write only the six declared outputs in an already-detached private candidate",
    )
    namespace = parser.parse_args()
    release = Path(namespace.release) if namespace.release else latest_release()
    receipt = unify_candidate_in_place(release) if namespace.candidate_in_place else unify(release)
    body = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if namespace.receipt:
        Path(namespace.receipt).write_text(body)
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
