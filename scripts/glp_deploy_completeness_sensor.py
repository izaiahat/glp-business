#!/usr/bin/env python3
"""Deterministic post-deploy completeness sensor for glpclinicscout.com.

WHY THIS EXISTS
---------------
The 2026-08-06 enrichment deploy returned PASS 31 seconds after it shipped.
The surface audit it leaned on filtered registry rows to `source_kind == "money"`,
so 8 newly wired programs were structurally invisible to it. The same night:
deep-research artifacts existed on disk but were never merged into
`program-content-ledger.json`, four live pages stated prices and shipping that
contradicted same-day research, and two side files had drifted ids.

Every one of those was silent. This sensor makes them loud. It asserts six
things and returns a single canonical JSON verdict:

  1. COVERAGE          audited count == registry live == ledger == build report
  1b. SURFACE           every live provider is on compare + quiz + calculator, or a
                        written per-provider exception says why (no class exemptions)
  1bb. THERAPEUTIC      evidence taxonomy, compare facets, and quiz eligibility form
                        one exact current-provider binding with no stale references
  1c. COUNT COPY        every provider count in RENDERED bytes == derived live count
  1d. DERIVED TALLIES   every provider / comparison / guide tally a reader sees equals
                        a value derivable from the shipped tree (no hand-typed totals)
  2. RESEARCH BINDING  research exists, is fresh, and is actually merged
  2b. FRESHNESS         no provider surface stamps "updated" older than the newest
                        dated evidence that same page renders
  3. PAGE-FACT         page copy matches the research artifact value-for-value
  4. IDENTITY          registry ids == eligibility ids == ledger ids
  4b. IDENTITY DEDUP    no surface list carries one provider twice under two casings;
                        a second registry row for one merchant must declare its alias
  5. LINK GRAPH        every live review page has >= 3 inbound internal links
  5b. SCORE BINDING     no live provider's review page ships a pending-score
                        placeholder while its score exists elsewhere in the release
  5c. PROVIDER LINKS    every approved-provider link in the built tree is monetized,
                        or carries an explicit recorded citation reason - and every
                        monetized link discloses itself with rel="sponsored"
  6. BASE CHAIN        content base deployment_id == live canonical deployment

Read-only: no Sheet writes, no provider writes, no outreach, no tracking-link
clicks, no secrets printed. The Cloudflare token is read from ~/.hermes/.env
for one GET and never echoed.

    python3 scripts/glp_deploy_completeness_sensor.py \
        [--release DIR] [--output receipt.json] [--offline] [--site URL]

Exit 0 = PASS, 1 = FAIL, 2 = usage/environment error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

BUSINESS = Path("/home/ubuntu/business")
if str(BUSINESS / "scripts") not in sys.path:
    sys.path.insert(0, str(BUSINESS / "scripts"))
from glp_current_content_base_schema import validate_pointer_shape  # noqa: E402
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
CONTENT_BASE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
SURFACE_AUDIT = BUSINESS / "scripts/affiliate_surface_coverage_audit.py"
ENV_FILE = Path("/home/ubuntu/.hermes/.env")
SITE = "https://glpclinicscout.com"
CF_ACCOUNT = "f860943dff399ffb1db0a28dcd3dadfd"
CF_PROJECT = "glp-clinic-scout"
SENSOR_VERSION = "glp-deploy-completeness-sensor-v1"

RESEARCH_MAX_AGE_DAYS = 30
INBOUND_LINK_FLOOR = 3

#: Where deep-research artifacts live. Any `*.json` directly under one of these
#: is indexed by its `program_id` field (falling back to the filename stem).
RESEARCH_ROOTS = (
    MACHINE_DIR / "deep-research-20260806",
    MACHINE_DIR / "g6-r1a-wirein-20260806/research",
)
#: Additional roots discovered by shape, so a new wave's research directory is
#: picked up without editing this file.
RESEARCH_GLOBS = ("deep-research-*", "*/research", "*/program-research")

#: research artifact field -> ledger keys that may carry the merged value.
#: Mirrors FIELD_ALIASES in affiliate_surface_coverage_audit.py; kept local so
#: the sensor stays independent of that script's internals.
LEDGER_ALIASES = {
    "monthly_price": ("research_monthly_price", "starting_price", "monthly_price"),
    "first_month_price": ("research_first_month_price", "first_month_price", "promo_price"),
    "states_served": ("research_states_served", "states_served", "states"),
    "insurance": ("research_insurance", "insurance"),
    "medication_forms": ("research_medication_forms", "meds_offered", "medication_forms"),
    "consultation_fee": ("research_consultation_fee", "consultation_fee", "membership_fee"),
    "shipping_wait": ("research_shipping_wait", "average_wait", "shipping_wait"),
    "cancellation": ("research_cancellation", "cancellation", "cancellation_policy"),
    "pharmacy_type": ("research_pharmacy_type", "pharmacy_type", "pharmacy"),
    "legitscript": ("research_legitscript", "legitscript", "legitscript_status"),
}

#: page_copy field -> the research fields that may attest its value tokens.
PAGE_FACT_MAP = {
    "pricing": ("first_month_price", "monthly_price", "consultation_fee"),
    "shipping": ("shipping_wait",),
    "cancellation": ("cancellation",),
}

EMPTYISH = re.compile(r"^\s*(not stated|unclear|unknown|none|null|n/?a|)\s*$", re.I)
_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
_PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s?%")
_DURATION = re.compile(
    r"\b([0-9]+(?:\s?[-–]\s?[0-9]+)?)\s*(hour|hr|day|business day|week|month|year)s?\b", re.I)
_SHIP_WORDS = re.compile(r"\b(overnight|same[- ]day|next[- ]day|expedited|free shipping)\b", re.I)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def check(name: str, ok: bool, **detail: Any) -> dict[str, Any]:
    return {"check": name, "status": "PASS" if ok else "FAIL", **detail}


def skipped(name: str, why: str, **detail: Any) -> dict[str, Any]:
    return {"check": name, "status": "SKIPPED", "reason": why, **detail}


def latest_release() -> Path:
    # A bulk filesystem touch is not a publication. Prefer the committed
    # content-base pointer; retain the legacy fallback for missing/bad targets.
    try:
        raw = load_json(CONTENT_BASE)
        value = raw.get("release_root") if isinstance(raw, dict) else None
        if isinstance(value, str) and value.strip():
            pointed = Path(value)
            if pointed.is_absolute() and (pointed / "data/program-registry.json").is_file():
                return pointed
    except (OSError, ValueError, TypeError):
        pass
    dirs = [p for p in RELEASES.iterdir() if (p / "data/program-registry.json").is_file()]
    if not dirs:
        raise RuntimeError("no release carries data/program-registry.json")
    return max(dirs, key=lambda p: p.stat().st_mtime)


def registry_rows(release: Path) -> list[dict[str, Any]]:
    raw = load_json(release / "data/program-registry.json")
    return raw if isinstance(raw, list) else (raw.get("programs") or [])


PROFILE = "glp-clinic-scout"

THERAPEUTIC_ALLOWED = {
    "molecule_tags": {"semaglutide", "tirzepatide", "retatrutide"},
    "brand_name_tags": {"wegovy", "zepbound", "mounjaro"},
    "delivery_form_tags": {"oral", "injectable"},
    "treatment_type_tags": {"compounded", "brand_name"},
}
THERAPEUTIC_REQUIRED_OPTIONS = (
    "semaglutide", "tirzepatide", "retatrutide", "wegovy", "zepbound",
    "mounjaro", "oral", "injectable",
)
THERAPEUTIC_QUIZ_OPTIONS = (
    "semaglutide", "tirzepatide", "retatrutide", "wegovy", "zepbound",
    "mounjaro", "oral", "injectable", "not_sure",
)
DYNAMIC_REQUIRED_SURFACES = ("compare", "quiz", "calculator", "counts", "suggestions")
RETATRUTIDE_AUTHORITIES = {
    "fda": "https://www.fda.gov/drugs/drug-alerts-and-statements/"
           "fdas-concerns-unapproved-glp-1-drugs-used-weight-loss",
    "lilly": "https://www.lilly.com/news/stories/what-to-know-about-retatrutide",
}


def _profile(row: dict[str, Any]) -> dict[str, Any]:
    return ((row.get("profiles") or {}).get(PROFILE) or {})


def render_id(row: dict[str, Any]) -> str:
    """The identity the ledger, the research artifacts, and the route agree on.

    A READY6 program is registered as `awin-128761` but renders at
    /reviews/coivas/ and is researched as coivas.json. Comparing registry ids
    against ledger keys reported that as identity drift when the two files
    actually agree (2026-08-06)."""
    return str(row.get("review_slug") or row.get("program_id"))


def _taxonomy_digest_basis(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "program_id", "registry_program_id", "provider", "render_label",
        "classification_state", "verified_as_of", "source_urls", "molecule_tags",
        "brand_name_tags", "delivery_form_tags", "treatment_type_tags",
        "treatment_type_state", "commercial_link_state", "evidence_bindings",
    )}


def _taxonomy_sha256(row: dict[str, Any]) -> str:
    body = json.dumps(_taxonomy_digest_basis(row), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _therapeutic_tokens(row: dict[str, Any]) -> list[str]:
    return sorted(set((row.get("molecule_tags") or [])
                      + (row.get("brand_name_tags") or [])
                      + (row.get("delivery_form_tags") or [])))


def _commercial_link_state(row: dict[str, Any]) -> str:
    if row.get("live_link_allowed") is not True or row.get("promotion_authorized") is not True:
        return "editorial_link_dark"
    try:
        from urllib.parse import urlparse
        return "live" if urlparse(str(row.get("affiliate_href") or "")).scheme == "https" else "editorial_link_dark"
    except (TypeError, ValueError):
        return "editorial_link_dark"


def _validate_dynamic_profile_projection(
        release: Path, registry: dict[str, Any], projected_taxonomy: dict[str, dict[str, Any]],
        now: dt.datetime, failures: list[str]) -> dict[str, dict[str, Any]] | None:
    """Validate the normalized v1 provider source before trusting its rendered bindings.

    The release's validator owns the field-addressable evidence contract. This sensor
    executes it in a separate Node process, then independently binds the validated
    identities and taxonomy to shipped HTML below. A missing validator is a failure,
    never a legacy fallback for a release that declares dynamic profiles.
    """
    document = registry.get("provider_dynamic_profiles")
    if document is None:
        return None
    if (not isinstance(document, dict)
            or document.get("schema") != "glp-provider-dynamic-profiles-v1"
            or document.get("version") != 1):
        failures.append("provider dynamic profile schema/version invalid")
        return {}
    if tuple(document.get("required_surfaces") or ()) != DYNAMIC_REQUIRED_SURFACES:
        failures.append("provider dynamic profile required surfaces are incomplete or reordered")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        failures.append("provider dynamic profile map is not an object")
        return {}
    if set(profiles) != set(projected_taxonomy):
        failures.append("provider dynamic profile membership does not equal canonical taxonomy projection")

    validator = release / "src/provider-dynamic-profiles.mjs"
    if not validator.is_file() or validator.is_symlink():
        failures.append("provider dynamic profile validator missing or not a regular file")
        return profiles
    node_source = r"""
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root = process.argv[1];
const asOf = process.argv[2];
const registry = JSON.parse(fs.readFileSync(path.join(root, 'data/program-registry.json'), 'utf8'));
const mod = await import(pathToFileURL(path.join(root, 'src/provider-dynamic-profiles.mjs')).href);
const validated = mod.validateProviderDynamicProfiles(registry, 'glp-clinic-scout', {asOf: new Date(asOf)});
process.stdout.write(JSON.stringify({canonical_ids: validated.canonicalIds, counts: validated.counts}));
"""
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "--eval", node_source, str(release),
             now.isoformat()], text=True, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"provider dynamic profile validator did not run: {type(exc).__name__}")
        return profiles
    if proc.returncode != 0:
        detail = " ".join(proc.stderr.strip().split())[-500:]
        failures.append(f"provider dynamic profile validator rejected release: {detail}")
        return profiles
    try:
        validated = json.loads(proc.stdout)
    except ValueError:
        failures.append("provider dynamic profile validator output is not JSON")
        return profiles
    if validated.get("canonical_ids") != sorted(profiles):
        failures.append("provider dynamic validator canonical identities disagree with profile map")
    if validated.get("counts") != document.get("expected_counts"):
        failures.append("provider dynamic validator counts disagree with sealed expected counts")

    for pid in sorted(set(profiles) & set(projected_taxonomy)):
        profile = profiles[pid]
        taxonomy = profile.get("taxonomy") if isinstance(profile, dict) else None
        expected = projected_taxonomy[pid]
        if not isinstance(taxonomy, dict):
            failures.append(f"{pid}: provider dynamic taxonomy missing")
            continue
        for field in THERAPEUTIC_ALLOWED:
            if taxonomy.get(field) != expected.get(field):
                failures.append(f"{pid}: provider dynamic taxonomy conflicts with evidence projection: {field}")
    return profiles


def _retatrutide_consumer_available(
        registry: dict[str, Any], dynamic_profiles: dict[str, dict[str, Any]] | None,
        now: dt.datetime, failures: list[str]) -> bool:
    """Derive availability from exact provider evidence plus both current authorities."""
    if dynamic_profiles is None:
        return True
    document = registry.get("consumer_therapeutic_authorities")
    molecule = ((document or {}).get("molecules") or {}).get("retatrutide") or {}
    if (not isinstance(document, dict)
            or document.get("schema") != "glp-consumer-therapeutic-authority-v1"
            or document.get("version") != 1
            or molecule.get("gate") !=
            "positive_provider_evidence_and_all_current_lawful_regulatory_authorities"):
        failures.append("retatrutide consumer authority schema/gate invalid")
        return False
    records = molecule.get("official_evidence")
    by_authority = {str(row.get("authority")): row for row in records or []
                    if isinstance(row, dict)}
    if set(by_authority) != set(RETATRUTIDE_AUTHORITIES) or len(records or []) != 2:
        failures.append("retatrutide consumer authority set is not exact FDA plus Lilly")
        return False
    max_age = document.get("freshness_max_days")
    if not isinstance(max_age, int) or not 1 <= max_age <= 30:
        failures.append("retatrutide consumer authority freshness policy invalid")
        max_age = 0
    lawful = True
    for authority, url in RETATRUTIDE_AUTHORITIES.items():
        record = by_authority[authority]
        text = str(record.get("evidence_text") or "")
        if (record.get("source_url") != url
                or hashlib.sha256(text.encode()).hexdigest() != record.get("evidence_sha256")):
            failures.append(f"retatrutide {authority} authority URL/evidence hash mismatch")
        try:
            age = (now.date() - dt.date.fromisoformat(str(record.get("verified_as_of")))).days
            if age < -1 or age > max_age:
                failures.append(f"retatrutide {authority} authority evidence is stale")
        except ValueError:
            failures.append(f"retatrutide {authority} authority date invalid")
        lawful = lawful and record.get("consumer_availability") is True
    provider_positive = any(
        "retatrutide" in (((profile or {}).get("taxonomy") or {}).get("molecule_tags") or [])
        for profile in dynamic_profiles.values())
    return provider_positive and lawful


def identity_key(name: str) -> str:
    """Case- and punctuation-insensitive brand key.

    "Shed" and "SHED" are one merchant wired in by two affiliate networks on two
    waves (2026-08-06). Comparing display names verbatim made them two providers
    on the compare table, in the calculator dropdown, and in every count."""
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _alias_declaration(row: dict[str, Any]) -> dict[str, Any]:
    return (_profile(row).get("canonical_identity") or {})


def is_alias_row(row: dict[str, Any]) -> bool:
    return str(_alias_declaration(row).get("role") or "") == "alias"


def _live_selector(row: dict[str, Any]) -> bool:
    profile = _profile(row)
    return (str(row.get("commercial_state") or "").lower() == "live"
            and profile.get("vertical_fit") is True and profile.get("wired") is True
            and (profile.get("surfaces") or {}).get("review") is True)


def live_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live per the site profile's own live_selector, one row per merchant.

    An alias row is a second network's offer for a merchant already counted. It
    keeps its review page but is never a second provider in a list or a tally."""
    return [row for row in rows if _live_selector(row) and not is_alias_row(row)]


def live_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({render_id(row) for row in live_rows(rows)})


def alias_ids(rows: list[dict[str, Any]]) -> dict[str, str]:
    """render identity -> canonical program_id, for every declared alias row."""
    return {render_id(row): str(_alias_declaration(row).get("canonical_program_id") or "")
            for row in rows if _live_selector(row) and is_alias_row(row)}


def outside_register_reasons(release: Path, rows: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Live review routes deliberately not yet tracked as programs, with the
    written reason recorded alongside them. A live route missing from BOTH the
    register and this list is a real gap and still fails."""
    reasons: dict[str, str] = {}
    for name in ("data/program-page-eligibility.json", "data/affiliate-activation-selection.json"):
        try:
            doc = load_json(release / name)
        except (OSError, ValueError):
            continue
        for entry in (doc.get("live_review_routes_outside_program_register") or []):
            pid = str(entry.get("program_id") or "")
            reason = str(entry.get("reason") or "").strip()
            if pid and len(reason) > 40:
                reasons[pid] = reason
    # The list is written with registry ids; callers compare render identities.
    # Record both spellings so a READY6 route excused as `katalys-1286` also
    # matches its render identity `sprout-health`.
    for row in rows or []:
        pid = str(row.get("program_id"))
        if pid in reasons:
            reasons[render_id(row)] = reasons[pid]
    return reasons


def ledger_entries(release: Path) -> dict[str, dict[str, Any]]:
    raw = load_json(release / "data/program-content-ledger.json")
    progs = raw.get("programs") if isinstance(raw, dict) else raw
    if isinstance(progs, dict):
        return {str(k): v for k, v in progs.items()}
    return {str(p.get("program_id")): p for p in (progs or [])}


def eligibility_ids(release: Path) -> list[str]:
    path = release / "data/program-page-eligibility.json"
    if not path.is_file():
        return []
    raw = load_json(path)
    progs = raw.get("programs") if isinstance(raw, dict) else raw
    if isinstance(progs, dict):
        return sorted(str(k) for k in progs)
    return sorted(str(p.get("program_id")) for p in (progs or []))


def build_report(release: Path) -> tuple[Path | None, dict[str, Any]]:
    for candidate in (release / "artifacts/ops/build-report.json",
                      release / "public/build-report.json",
                      release / "build-report.json"):
        if candidate.is_file():
            try:
                return candidate, load_json(candidate)
            except ValueError:
                return candidate, {}
    return None, {}


def load_env_value(key: str) -> str | None:
    """Read one value from the Hermes env file. Never logged, never returned upward."""
    try:
        fd = os.open(ENV_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        text = os.read(fd, info.st_size + 1).decode("utf-8", "ignore")
    finally:
        os.close(fd)
    for raw in text.splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        name, value = raw.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# research index
# ---------------------------------------------------------------------------
def research_roots() -> list[Path]:
    roots = [p for p in RESEARCH_ROOTS if p.is_dir()]
    for pattern in RESEARCH_GLOBS:
        for path in MACHINE_DIR.glob(pattern):
            if path.is_dir() and path not in roots:
                roots.append(path)
    return roots


def _artifact_date(doc: dict[str, Any]) -> str | None:
    for key in ("captured_at_utc", "researched_at_utc", "generated_at_utc", "as_of"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    dates = [f.get("verified_as_of") for f in (doc.get("fields") or {}).values()
             if isinstance(f, dict) and isinstance(f.get("verified_as_of"), str)]
    return max(dates) if dates else None


def research_index() -> dict[str, dict[str, Any]]:
    """program_id -> {path, doc, captured_at}. Newest capture wins."""
    index: dict[str, dict[str, Any]] = {}
    for root in research_roots():
        for path in sorted(root.glob("*.json")):
            try:
                doc = load_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("fields"), dict):
                continue
            pid = str(doc.get("program_id") or path.stem)
            captured = _artifact_date(doc) or ""
            prior = index.get(pid)
            if prior is None or captured > (prior.get("captured_at") or ""):
                index[pid] = {"path": str(path), "doc": doc, "captured_at": captured}
    return index


def _age_days(stamp: str) -> float | None:
    text = (stamp or "").strip().replace("Z", "+00:00")
    for parser in (dt.datetime.fromisoformat,):
        try:
            when = parser(text)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400.0
    try:
        when = dt.datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400.0


def _ledger_value(entry: dict[str, Any], field: str) -> Any:
    for alias in LEDGER_ALIASES.get(field, ()):  # first non-empty alias wins
        value = entry.get(alias)
        if isinstance(value, dict):
            value = value.get("value")
        if value is not None and not EMPTYISH.fullmatch(str(value)):
            return value
    return None


# ---------------------------------------------------------------------------
# value tokens (page-fact consistency)
# ---------------------------------------------------------------------------
def value_tokens(text: str) -> set[str]:
    """Comparable factual tokens: money, percentages, durations, shipping words.

    Formatting is normalised away ("$1,200.00" and "$1200" are the same token);
    values are not.
    """
    blob = str(text or "")
    tokens: set[str] = set()
    for amount in _MONEY.findall(blob):
        clean = amount.replace(",", "")
        try:
            tokens.add(f"${float(clean):.2f}")
        except ValueError:
            continue
    for pct in _PERCENT.findall(blob):
        try:
            tokens.add(f"{float(pct):g}%")
        except ValueError:
            continue
    for number, unit in _DURATION.findall(blob):
        norm = re.sub(r"\s*[-–]\s*", "-", number.strip())
        tokens.add(f"{norm} {unit.lower().rstrip('s')}")
    for word in _SHIP_WORDS.findall(blob):
        tokens.add(re.sub(r"[- ]", "-", word.lower()))
    return tokens


def research_text(doc: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts: list[str] = []
    for name in fields:
        field = (doc.get("fields") or {}).get(name)
        if not isinstance(field, dict):
            continue
        for key in ("value", "quote", "note"):
            value = field.get(key)
            if isinstance(value, str):
                parts.append(value)
    return " \n".join(parts)


# ---------------------------------------------------------------------------
# the six checks
# ---------------------------------------------------------------------------
def _audit_supports(flag: str) -> bool:
    """The surface audit is owned by the coverage lane; probe rather than assume."""
    try:
        return flag in SURFACE_AUDIT.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def check_coverage(release: Path, rows: list[dict[str, Any]], ledger: dict[str, Any],
                   offline: bool) -> dict[str, Any]:
    audit_out = Path(tempfile.gettempdir()) / f"glp-sensor-surface-{os.getpid()}.json"
    command = [sys.executable, str(SURFACE_AUDIT), "--output", str(audit_out)]
    if _audit_supports("--release"):
        # Audit the same tree the sensor is judging; otherwise the audit picks
        # the newest release by mtime and the two can silently diverge.
        command += ["--release", str(release)]
    if not offline:
        command.append("--live")
    try:
        proc = subprocess.run(command, cwd=str(BUSINESS), text=True,
                              capture_output=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("coverage", False, error=f"surface_audit_did_not_run: {exc}")
    if not audit_out.is_file():
        return check("coverage", False, error="surface_audit_wrote_no_receipt",
                     exit_code=proc.returncode, stderr_tail=proc.stderr[-800:])
    try:
        audit = load_json(audit_out)
    except ValueError as exc:
        return check("coverage", False, error=f"surface_audit_receipt_unparseable: {exc}")
    finally:
        try:
            audit_out.unlink()
        except OSError:
            pass

    audited_ids = set(audit.get("programs") or {})
    excused = outside_register_reasons(release, rows)
    live_render_ids = live_ids(rows)
    outside = sorted(pid for pid in live_render_ids if pid not in ledger)
    # Counts are compared over the PROGRAM REGISTER. Live routes outside it are
    # listed, and an unexplained one fails below — they are never just subtracted.
    registry_live = len([pid for pid in live_render_ids if pid in ledger])
    # A declared alias is a second network's row for a merchant already counted.
    # Every denominator here counts MERCHANTS, so the alias comes out of all of
    # them at once rather than out of one and not the others.
    alias_render = set(alias_ids(rows))
    alias_all = alias_render | {str(r.get("program_id")) for r in rows
                                if render_id(r) in alias_render}
    # The ledger is a frozen editorial corpus, not the live denominator. Only
    # registered immutable HOLD receipts, proven against current emitted bytes,
    # can remove an editorial identity; false promotion flags alone never can.
    held_proofs = _receipted_editorial_holds(release, rows, set(ledger))
    held_ids = {pid for pid, proof in held_proofs.items() if proof["exempt"]}
    ledger_raw_ids = set(ledger) - alias_all
    ledger_live_ids = ledger_raw_ids - held_ids
    ledger_count = len(ledger_live_ids)
    report_path, report = build_report(release)
    build_live = report.get("live_programs")

    audited = audit.get("program_register_programs", len(audited_ids))
    audited -= len(audited_ids & alias_all) if audited_ids else len(alias_all)
    counts = {
        "surface_audit_audited": audited,
        "registry_live": registry_live,
        "content_ledger": ledger_count,
        "build_report_live_programs": build_live,
        "declared_alias_rows": sorted(alias_render),
    }
    failures: list[str] = [
        f"{pid}: receipted editorial HOLD proof failed: {reason}"
        for pid, proof in held_proofs.items() for reason in proof["failures"]
    ]
    expected_ledger_ids = set(live_render_ids) & set(ledger)
    if ledger_live_ids != expected_ledger_ids:
        failures.append("content ledger live membership differs from registry: "
                        f"missing={sorted(expected_ledger_ids - ledger_live_ids)}, "
                        f"unexpected={sorted(ledger_live_ids - expected_ledger_ids)}")
    slug_of = {str(r.get("program_id")): render_id(r) for r in rows}
    audited_render_ids = {slug_of.get(pid, pid) for pid in audited_ids} - alias_all
    audited_register_ids = audited_render_ids - set(outside)
    if audited_register_ids != expected_ledger_ids:
        failures.append("surface audit live membership differs from registry: "
                        f"missing={sorted(expected_ledger_ids - audited_register_ids)}, "
                        f"unexpected={sorted(audited_register_ids - expected_ledger_ids)}")
    counts.update(content_ledger_raw=len(ledger_raw_ids),
                  content_ledger_editorial_held=len(ledger_raw_ids & held_ids),
                  receipted_editorial_held_ids=sorted(held_ids))
    if build_live is None:
        failures.append("build report does not record live_programs"
                        f" ({report_path or 'build-report.json missing'})")
        distinct = {audited, registry_live, ledger_count}
    else:
        distinct = {audited, registry_live, ledger_count, int(build_live)}
    if len(distinct) != 1:
        failures.append("program counts disagree — a filter or a stale artifact is hiding "
                        f"programs: {counts}")
    slug_of = {str(r.get("program_id")): render_id(r) for r in rows}
    excused_ids = set(excused) | {slug_of.get(pid, pid) for pid in excused}
    unexplained = [pid for pid in outside if pid not in excused_ids]
    if unexplained:
        failures.append("live review routes are outside the program register with no written "
                        f"reason recorded: {unexplained}")
    counts["live_outside_program_register"] = len(outside)
    audited_release = str(audit.get("release") or "")
    if audited_release and audited_release != release.name:
        failures.append(f"surface audit audited release {audited_release!r} but the sensor "
                        f"target is {release.name!r}")
    audit_failures = audit.get("failures") or []
    if audit_failures:
        failures.append(f"surface audit reports {len(audit_failures)} gap(s); a deploy is not "
                        "complete while the audit is red")
    return check("coverage", not failures, counts=counts,
                 surface_audit_status=audit.get("status"),
                 surface_audit_failure_count=len(audit_failures),
                 surface_audit_failures=audit_failures[:20],
                 build_report=str(report_path) if report_path else None,
                 failures=failures)


SURFACE_CHECKS = ("compare", "quiz", "calculator")
#: Copy patterns that state a provider count in prose. A rendered page may only
#: state the derived live count; any other number here is drift by definition.
#: The lookbehind matters: without it "GLP-1 programs" reads as "1 programs" and
#: the sensor cries drift on every page that names the drug class.
COUNT_COPY = re.compile(
    r"(?<![-‑\d])(\d{1,3})\s+(?:additional\s+|more\s+|other\s+|curated\s+|online\s+|GLP[-‑]?1\s+)*"
    r"(providers|programs|clinics)\b", re.I)
NUMBER_WORDS = {
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty-two": 22,
    "twenty-three": 23, "twenty-four": 24, "twenty-five": 25, "twenty-six": 26,
    "twenty-seven": 27, "twenty-eight": 28, "twenty-nine": 29, "thirty": 30,
}
WORD_COPY = re.compile(
    r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\s+"
    r"(?:additional\s+|more\s+|other\s+|curated\s+|online\s+|GLP[-‑]?1\s+)*"
    r"(providers|programs|clinics)\b", re.I)
#: Pages whose provider count a reader reads as "how many providers this site covers".
COUNT_COPY_ROUTES = (
    "index.html",
    "providers/index.html",
    "best/online-glp-1-providers/index.html",
    "cost/online-glp-1-programs/index.html",
    "best/expanded-curated-glp-1-programs/index.html",
    "tools/glp-1-cost-calculator/index.html",
    "quiz/provider-fit/index.html",
    "how-we-rate/index.html",
)
TAGS = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>|<[^>]+>", re.S)
PROVIDER_LOAD_MORE_BATCH = 12


def option_c_provider_card_contract(release: Path) -> dict[str, Any] | None:
    """Return the shipped Option C provider-card contract, if this release declares it.

    Option C deliberately omits evidence-ineligible providers from the default
    comparison grid while preserving their detail pages and registry rows.  The
    deploy sensor must read that exact shipped authority instead of treating the
    omissions as silent surface loss.
    """
    path = release / "data/glp-design-placeholder-manifest.json"
    if not path.is_file():
        return None
    document = load_json(path)
    decision = document.get("card_content_decision") or {}
    if decision.get("chosen_option") != "C":
        return None
    contract = decision.get("provider_contract")
    if not isinstance(contract, dict):
        raise ValueError("option_c_provider_contract_missing")
    if contract.get("core_fields") != [
            "display_name", "meds_offered", "total_monthly_cost", "cancellation"]:
        raise ValueError("option_c_provider_core_fields_invalid")
    excluded = contract.get("excluded_program_ids")
    eligible = contract.get("eligible_entities")
    total = contract.get("total_entities")
    if (not isinstance(excluded, list) or not excluded
            or len(excluded) != len(set(excluded))
            or not all(isinstance(pid, str) and pid for pid in excluded)
            or not isinstance(eligible, int) or not isinstance(total, int)
            or eligible + len(excluded) != total):
        raise ValueError("option_c_provider_membership_invalid")
    return contract


def _visible_text(html: str) -> str:
    return " ".join(TAGS.sub(" ", html).split())


def check_surface_completeness(release: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every live provider is on every ranked surface, or a written exception says why.

    THE BLIND SPOT THIS KILLS
    -------------------------
    The previous sensor only ever asked whether a provider had a REVIEW page. On
    2026-08-06 that let 16 live providers sit off the comparison page, the
    calculator and the quiz while the sensor returned PASS, because those
    surfaces were considered the money class's business. There is no money class
    any more. A live provider missing from a surface with no written per-provider
    reason on file is a FAIL, whatever lane wired it in.
    """
    failures: list[str] = []
    per_program: dict[str, Any] = {}
    try:
        registry = load_json(release / "data/program-registry.json")
        money = load_json(release / "data/money-page-specs.json")
        quiz = load_json(release / "data/quiz-config.json")
        option_c_contract = option_c_provider_card_contract(release)
    except (OSError, ValueError) as exc:
        return check("surface_completeness", False, error=f"surface_inputs_unreadable: {exc}")
    option_c_excluded = set((option_c_contract or {}).get("excluded_program_ids") or [])

    # Read the SHIPPED BYTES, not the data files. A registry flag flipped off while
    # the row stays in money-page-specs.json looks fine in data and disappears from
    # the page, which is precisely the failure mode being sensed. Data files are the
    # fallback only when a page was not built.
    def rendered_ids(route: str, pattern: str, fallback: set[str]) -> set[str]:
        page = release / "public" / route
        if not page.is_file():
            return fallback
        return set(re.findall(pattern, page.read_text(errors="replace")))

    compare_surface = "best/online-glp-1-providers/index.html"
    if not (release / "public" / compare_surface).is_file():
        # 2026-08-27 redesign: compare folded into the providers hub.
        compare_surface = "providers/index.html"
    compare_ids = rendered_ids(
        compare_surface, r'data-(?:program|provider-record)="([a-z0-9-]+)"',
        {str(row.get("program_id")) for page in money.get("pages") or [] for row in (page.get("rows") or [])})
    quiz_ids = rendered_ids(
        "quiz/provider-fit/index.html", r'"program_id":"([a-z0-9-]+)"',
        {str(c.get("program_id")) for c in (quiz.get("candidates") or [])})
    calculator_ids = rendered_ids(
        "tools/glp-1-cost-calculator/index.html", r'<option value="([a-z0-9-]+)"', set())
    rendered = {"compare": compare_ids, "quiz": quiz_ids, "calculator": calculator_ids}

    by_render = {render_id(row): row for row in rows}
    for pid in live_ids(rows):
        row = by_render[pid]
        profile = _profile(row)
        exceptions = profile.get("exceptions") or {}
        surfaces = profile.get("surfaces") or {}
        state: dict[str, str] = {}
        for surface in SURFACE_CHECKS:
            present = pid in rendered[surface] or str(row.get("program_id")) in rendered[surface]
            reason = str((exceptions.get(surface) or {}).get("reason") or "").strip()
            excused = (exceptions.get(surface) or {}).get("enabled") is True and len(reason) > 40
            if present:
                state[surface] = "present"
                if excused:
                    failures.append(f"{pid}: on {surface} AND carrying a written exception for it "
                                    "— one of the two is a lie")
            elif excused:
                state[surface] = "excepted"
            elif surface == "compare" and pid in option_c_excluded:
                state[surface] = "option_c_evidence_ineligible"
            else:
                state[surface] = "MISSING"
                failures.append(
                    f"{pid}: live but absent from {surface} with no written per-provider "
                    f"exception (registry surfaces.{surface}={surfaces.get(surface)!r}). "
                    "A wire-in lane is not a reason.")
        per_program[pid] = state
    return check("surface_completeness", not failures,
                 live_programs=len(by_render), surfaces_checked=list(SURFACE_CHECKS),
                 rendered_counts={k: len(v) for k, v in rendered.items()},
                 option_c_provider_contract=option_c_contract,
                 per_program=per_program, failures=failures)


# 2026-08-27 redesign: each surface names its route CANDIDATES, oldest first; the
# first one built in the release carries the contract. Pre-redesign releases keep
# resolving to the legacy routes; the redesign folds compare+cost into /providers/.
AFFILIATE_SURFACE_ROUTES = {
    "compare": ("best/online-glp-1-providers/index.html", "providers/index.html"),
    "cost": ("cost/online-glp-1-programs/index.html", "providers/index.html"),
    "quiz": ("quiz/provider-fit/index.html",),
    "calculator": ("tools/glp-1-cost-calculator/index.html",),
}


def _surface_route_path(release, routes):
    for route in routes:
        path = release / "public" / route
        if path.is_file():
            return route, path
    return routes[0], release / "public" / routes[0]
AWIN_ITEM1_IDS = {
    "53271": "drhouse", "115383": "biltrx",
    "126727": "measured", "128203": "serena-health", "128761": "coivas",
}
AWIN_ITEM1_CALCULATOR_IDS = frozenset({"53271", "115383", "128203", "128761"})
AWIN_ITEM1_FORBIDDEN_CALCULATOR_IDS = frozenset({"126727"})
AWIN_ITEM1_FORBIDDEN_ALL_SURFACE_IDS = frozenset({"126727"})
# 2026-08-26 best-terms reconciliation: Shed remains in the exact denominator,
# but its only authorized live route is whichever rail currently wins. Awin 80963
# remains an approved tracking-ready relationship and is deliberately absent.
# The literal below is the LAST-RESORT default only; _shed_best_terms_route()
# derives the live pair from the release's own activation-selection authority so
# a provider-wide payout switch moves the sensor's expectation with it instead of
# leaving a pinned pair the switch has already superseded.
SHED_BEST_TERMS_ROUTE = ("shedrx", "katalys-1516")


def _shed_best_terms_route(release: Path) -> tuple[str, str]:
    """Shed's currently authorized (program, route) pair, from release data."""
    path = Path(release) / "data/affiliate-activation-selection.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SHED_BEST_TERMS_ROUTE

    def rows(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from rows(item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
                yield from rows(item)

    program = SHED_BEST_TERMS_ROUTE[0]
    for row in rows(document):
        if str(row.get("program_id") or "") != program:
            continue
        network = str(row.get("network") or "").strip().lower()
        network_program_id = str(row.get("network_program_id") or "").strip()
        if network and network_program_id:
            return (program, f"{network}-{network_program_id}")
    return SHED_BEST_TERMS_ROUTE
AFFILIATE_TRACKING_PROVIDER_IDS = frozenset({*AWIN_ITEM1_IDS.values(), "shedrx"})


def _awin_id_from_href(href: str) -> str | None:
    try:
        from urllib.parse import parse_qsl, urlsplit
        if not isinstance(href, str) or href != href.strip() or any(
                ord(character) < 32 or ord(character) == 127 for character in href):
            return None
        decoded = html.unescape(href)
        if decoded != decoded.strip() or any(
                ord(character) < 32 or ord(character) == 127 for character in decoded):
            return None
        parsed = urlsplit(decoded)
        # netloc is intentionally compared byte-for-byte. ``hostname`` lowercases
        # and would otherwise let authority case drift, trailing dots, userinfo,
        # or explicit ports masquerade as the pinned host.
        if (parsed.scheme != "https" or parsed.netloc != "www.awin1.com"
                or parsed.hostname != "www.awin1.com" or parsed.username is not None
                or parsed.password is not None or parsed.port is not None
                or parsed.fragment or parsed.path not in ("/awclick.php", "/cread.php")):
            return None
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
        identity_keys = {"id", "awinaffid", "mid", "awinmid"}
        if any(key.lower() in identity_keys and key not in identity_keys for key, _ in query_pairs):
            return None
        publisher_values = [value for key, value in query_pairs if key in ("id", "awinaffid")]
        advertiser_values = [value for key, value in query_pairs if key in ("mid", "awinmid")]
        if publisher_values != ["2979387"]:
            return None
        if len(advertiser_values) != 1 or not advertiser_values[0].strip():
            return None
        return advertiser_values[0]
    except (TypeError, ValueError):
        return None


def _katalys_id_from_href(href: str) -> str | None:
    try:
        from urllib.parse import parse_qsl, urlsplit
        if not isinstance(href, str) or href != href.strip() or any(
                ord(character) < 32 or ord(character) == 127 for character in href):
            return None
        decoded = html.unescape(href)
        parsed = urlsplit(decoded)
        if (parsed.scheme != "https" or parsed.netloc != "track.revoffers.com"
                or parsed.hostname != "track.revoffers.com" or parsed.username is not None
                or parsed.password is not None or parsed.port is not None
                or parsed.fragment or parsed.path != "/aff_c"):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
        identity_keys = {"offer_id", "aff_id"}
        if any(key.lower() in identity_keys and key not in identity_keys for key, _ in pairs):
            return None
        offer_ids = [value for key, value in pairs if key == "offer_id"]
        publisher_ids = [value for key, value in pairs if key == "aff_id"]
        if offer_ids != ["1516"] or publisher_ids != ["13031"]:
            return None
        return offer_ids[0]
    except (TypeError, ValueError):
        return None


def _affiliate_route_id(href: str) -> str | None:
    awin = _awin_id_from_href(href)
    if awin:
        return awin
    katalys = _katalys_id_from_href(href)
    return f"katalys-{katalys}" if katalys else None


def _go_route_slug(href: str) -> str | None:
    """Parse one local first-party /go route without requesting or following it."""
    try:
        from urllib.parse import urlsplit
        decoded = html.unescape(href)
        parsed = urlsplit(decoded)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        match = re.fullmatch(r"/go/([a-z0-9]+(?:-[a-z0-9]+)*)", parsed.path)
        return match.group(1) if match else None
    except (TypeError, ValueError):
        return None


def _go_receipt_date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
        return False
    try:
        return dt.date.fromisoformat(value) <= dt.datetime.now(dt.timezone.utc).date()
    except ValueError:
        return False


def _go_receipt_source(value: Any) -> bool:
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(value if isinstance(value, str) else "")
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _go_redirector_terms_allowed(release: Path, program: dict[str, Any]) -> bool:
    """Accept express terms OR exact adopted silent-terms risk authority.

    Risk acceptance remains explicitly NOT express network permission. Unlike a
    manifest-only boolean, the second branch joins the shipped selection bytes,
    exact terms-ledger row and original operator ruling hash. No new authority is
    created; missing, stale, held or tampered bindings fail closed.
    """
    if program.get("redirector_tos_verified") is True:
        return (_go_receipt_source(program.get("redirector_tos_source_url"))
                and _go_receipt_date(program.get("redirector_tos_verified_as_of")))
    if program.get("redirector_tos_verified") is not False:
        return False
    try:
        risk = program["redirector_risk_acceptance"]
        selection_path = release / "data/go-selection-receipt.json"
        selection_bytes = selection_path.read_bytes()
        selection_sha = hashlib.sha256(selection_bytes).hexdigest()
        selection = json.loads(selection_bytes)
        ledger = load_json(release / "data/go-terms-review-ledger.json")
        rows = selection["programs"]
        pid = program["program_id"]
        if (selection.get("schema") != "glp-go-selection-v1"
                or selection.get("status") != "ADOPTED_CANDIDATE_NOT_RELEASE_ACCEPTANCE"
                or not isinstance(rows, list) or type(selection.get("count")) is not int
                or selection["count"] != len(rows)
                or len({row["program_id"] for row in rows}) != len(rows)
                or ledger.get("schema") != "glp-go-terms-review-ledger-v1"
                or ledger.get("selection_sha256") != selection_sha
                or ledger.get("programs", {}).get(pid) != risk):
            return False
        row = next(row for row in rows if row["program_id"] == pid)
        ruling = selection["operator_ruling"]
        if (row.get("disposition") != "ADOPT"
                or row.get("network") != program.get("network")
                or row.get("render_id") != (program.get("route_slug") or pid)
                or row.get("route") != program.get("route")
                or row.get("issued_link") != program.get("tracking", {}).get("base_url")
                or any(row.get(key) is not True for key in (
                    "promotion_authorized", "constructor_verified", "advertiser_permission_verified",
                    "encoding_verified", "privacy_qa_passed"))
                or risk.get("schema") != "go-redirector-risk-acceptance-v1"
                or risk.get("risk_accepted") is not True
                or risk.get("scope") != "first-party-redirect-and-documented-subids"
                or risk.get("express_network_permission_proven") is not False
                or risk.get("program_id") != pid
                or risk.get("network") != program.get("network")
                or risk.get("tracking_base_url") != row.get("issued_link")
                or risk.get("operator_selection_receipt_sha256") != selection_sha
                or program.get("operator_selection_receipt_sha256") != selection_sha
                or risk.get("operator_decision_sha256") != ruling.get("sha256")
                or not re.fullmatch(r"[a-f0-9]{64}", str(ruling.get("sha256") or ""))
                or hashlib.sha256(Path(ruling["path"]).read_bytes()).hexdigest() != ruling["sha256"]
                or not _go_receipt_date(risk.get("accepted_as_of"))
                or not isinstance(risk.get("limitation"), str) or not risk["limitation"].strip()):
            return False
        if _go_receipt_source(risk.get("terms_source_url")):
            return True
        artifact = Path(risk["terms_source_artifact"])
        return (bool(re.fullmatch(r"[a-f0-9]{64}", str(risk.get("terms_source_artifact_sha256") or "")))
                and hashlib.sha256(artifact.read_bytes()).hexdigest() == risk["terms_source_artifact_sha256"])
    except (OSError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return False


def _active_go_route_ids(release: Path) -> dict[str, str]:
    """Return local /go slug -> governed upstream id for fully promoted routes.

    This reads the build's own manifest bytes. It never requests a tracking URL.
    The gate mirrors src/worker.mjs: a candidate or partially promoted route does
    not count as monetized merely because an href exists.
    """
    path = release / "artifacts/ops/worker-manifest.staging.json"
    try:
        manifest = load_json(path)
    except (OSError, ValueError):
        return {}
    if (manifest.get("schema") != "affiliate-worker-staging-v3-registry"
            or manifest.get("site_profile") != PROFILE
            or not isinstance(manifest.get("programs"), list)):
        return {}
    routes: dict[str, str] = {}
    for program in manifest["programs"]:
        slug = str(program.get("route_slug") or program.get("program_id") or "")
        active = (
            program.get("route") == f"/go/{slug}"
            and program.get("link_mode") == "server_redirect"
            and program.get("go_route_wired") is True
            and program.get("live_link_allowed") is True
            and program.get("promotion_authorized") is True
            and program.get("status") == "live_operator_approved"
            and bool(program.get("operator_selection_receipt_sha256"))
            and program.get("constructor_verified") is True
            and program.get("advertiser_permission_verified") is True
            and program.get("encoding_verified") is True
            and program.get("privacy_qa_passed") is True
            and _go_redirector_terms_allowed(release, program)
        )
        route_id = _affiliate_route_id(str((program.get("tracking") or {}).get("base_url") or ""))
        if active and route_id:
            routes[slug] = route_id
    return routes


def _include_deferred_provider_payloads(release: Path, page_html: str) -> tuple[str, list[str]]:
    """Hydrate local deferred card bodies for static tracking inspection."""
    failures: list[str] = []
    bodies = [page_html]
    sources = re.findall(
        r'<script\b[^>]*data-provider-card-payloads[^>]*data-src="([^"]+)"[^>]*>',
        page_html,
    )
    for source in sorted(set(sources)):
        if not re.fullmatch(r"/assets/provider-card-payloads\.[a-f0-9]{16}\.json", source):
            failures.append(f"deferred provider-card payload path invalid: {source}")
            continue
        path = release / "public" / source.lstrip("/")
        try:
            payload = load_json(path)
        except (OSError, ValueError) as exc:
            failures.append(f"deferred provider-card payload unreadable: {source}: {exc}")
            continue
        if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
            failures.append(f"deferred provider-card payload invalid: {source}")
            continue
        bodies.extend(payload)
    return "\n".join(bodies), failures


def _rendered_affiliate_pairs(surface: str, page_html: str,
                              go_route_ids: dict[str, str] | None = None) -> set[tuple[str, str]]:
    """Return exact (canonical provider id, governed route id) tracked pairs."""
    pairs: set[tuple[str, str]] = set()
    go_route_ids = go_route_ids or {}

    def route_id(href: str) -> str | None:
        direct = _affiliate_route_id(href)
        if direct:
            return direct
        slug = _go_route_slug(href)
        return go_route_ids.get(slug or "")

    if surface in ("compare", "cost"):
        # 2026-08-27: redesign CTAs carry data-program on the anchor itself
        # (hub cards and provider-detail rails), not only inside legacy rows.
        for tag in re.findall(r'<a\b[^>]*data-program="[a-z0-9-]+"[^>]*>', page_html):
            attrs = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', tag))
            tracked_id = route_id(attrs.get("href", ""))
            if tracked_id and attrs.get("data-program"):
                pairs.add((attrs["data-program"], tracked_id))
        card_blocks = re.findall(r'<tr\b[^>]*data-program="([a-z0-9-]+)"[^>]*>(.*?)</tr>', page_html, re.S)
        # 2026-08-27 redesign: provider cards are <article data-program> blocks.
        card_blocks += re.findall(r'<article\b[^>]*data-program="([a-z0-9-]+)"(.*?)</article>', page_html, re.S)
        for provider_id, body in card_blocks:
            for href in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>', body, re.S):
                tracked_id = route_id(href)
                if tracked_id:
                    pairs.add((provider_id, tracked_id))
    elif surface == "quiz":
        # 2026-08-27 redesign: the quiz payload moved to #phase4-quiz-data with
        # candidate_records{program_id: {affiliate_href, has_live_link}}.
        phase4 = re.search(r'<script[^>]*id="phase4-quiz-data"[^>]*>(.*?)</script>', page_html, re.S)
        if phase4:
            try:
                payload = json.loads(html.unescape(phase4.group(1)))
            except (TypeError, ValueError):
                payload = {}
            for program_id, row in (payload.get("candidate_records") or {}).items():
                tracked_id = route_id(str(row.get("affiliate_href") or ""))
                if row.get("has_live_link") is True and tracked_id:
                    pairs.add((str(program_id), tracked_id))
        match = re.search(r'<script[^>]*id="candidate-data"[^>]*>(.*?)</script>', page_html, re.S)
        if match:
            try:
                candidates = json.loads(html.unescape(match.group(1)))
            except (TypeError, ValueError):
                candidates = []
            for row in candidates:
                tracked_id = route_id(str(row.get("affiliate_url") or ""))
                if row.get("has_live_link") is True and tracked_id:
                    pairs.add((str(row.get("program_id")), tracked_id))
    elif surface == "calculator":
        match = re.search(r'const links=new Map\((\[.*?\])\.map\(link=>', page_html, re.S)
        if match:
            try:
                links = json.loads(match.group(1))
            except (TypeError, ValueError):
                links = []
            for row in links:
                tracked_id = route_id(str(row.get("affiliate_href") or ""))
                if row.get("live") is True and tracked_id:
                    pairs.add((str(row.get("program_id")), tracked_id))
    return pairs


def check_affiliate_surface_tracking(release: Path) -> dict[str, Any]:
    """Prove the exact hold-aware affiliate surface denominator and route winner."""
    all_required = {(canonical, awin) for awin, canonical in AWIN_ITEM1_IDS.items()
                    if awin not in AWIN_ITEM1_FORBIDDEN_ALL_SURFACE_IDS}
    shed_route = _shed_best_terms_route(release)
    all_required.add(shed_route)
    all_forbidden = {(AWIN_ITEM1_IDS[awin], awin)
                     for awin in AWIN_ITEM1_FORBIDDEN_ALL_SURFACE_IDS}
    calculator_required = {(AWIN_ITEM1_IDS[awin], awin)
                           for awin in AWIN_ITEM1_CALCULATOR_IDS}
    calculator_required.add(shed_route)
    calculator_forbidden = {(AWIN_ITEM1_IDS[awin], awin)
                            for awin in AWIN_ITEM1_FORBIDDEN_CALCULATOR_IDS}
    # Generic unit fixtures and sister verticals do not carry this fixed Awin scope.
    # As soon as any target identity appears, however, the exact Bridge matrix is
    # mandatory; a partial wave cannot evade the check by omitting metadata.
    observed_target_ids: set[str] = set()
    for routes in AFFILIATE_SURFACE_ROUTES.values():
        _, path = _surface_route_path(release, routes)
        if path.is_file():
            body = path.read_text(errors="replace")
            observed_target_ids.update(pid for pid in AFFILIATE_TRACKING_PROVIDER_IDS if pid in body)
    if not observed_target_ids:
        return skipped("affiliate_surface_tracking",
                       "Awin Item 1 fixed-denominator identities not present in this release")
    failures: list[str] = []
    per_surface: dict[str, Any] = {}
    go_route_ids = _active_go_route_ids(release)
    for surface, routes in AFFILIATE_SURFACE_ROUTES.items():
        route, path = _surface_route_path(release, routes)
        if not path.is_file():
            failures.append(f"{surface}: built route missing: {route}")
            per_surface[surface] = {"route": route, "provider_presence": "MISSING",
                                    "tracked_link_coverage": "MISSING"}
            continue
        page_html = path.read_text(errors="replace")
        if surface == "calculator":
            present_ids = set(re.findall(r'<option value="([a-z0-9-]+)"', page_html))
            required = calculator_required
            forbidden = calculator_forbidden
        elif surface == "quiz":
            present_ids = set(re.findall(r'"program_id":"([a-z0-9-]+)"', page_html))
            required = all_required
            forbidden = all_forbidden
        else:
            # 2026-08-27: legacy tables use <tr data-program>; the redesigned
            # providers hub renders <article data-phase3-provider data-program>
            # plus <a data-provider-record> links for every governed identity. A
            # provider without priced matrix rows has no hub CARD; its tracked
            # link lives on its own detail page, which carries the contract.
            present_ids = set(re.findall(r'<tr\b[^>]*data-program="([a-z0-9-]+)"', page_html))
            present_ids |= set(re.findall(r'<article\b[^>]*data-program="([a-z0-9-]+)"', page_html))
            present_ids |= set(re.findall(r'data-provider-record="([a-z0-9-]+)"', page_html))
            page_html, deferred_failures = _include_deferred_provider_payloads(release, page_html)
            failures.extend(f"{surface}: {failure}" for failure in deferred_failures)
            # DEFECT 2026-09-03: this used to skip the detail page whenever the
            # route id appeared ANYWHERE in the hub as a raw substring. The hub's
            # schema.org ItemList carries ".../providers/katalys-1516/", so Shed
            # matched on a breadcrumb URL, the fallback never ran, and the sensor
            # reported shedrx:katalys-1516 missing while the link was live on the
            # provider detail page all along. Key on the rendered PAIR instead:
            # only a genuinely rendered live link counts as already present.
            rendered_so_far = _rendered_affiliate_pairs(surface, page_html, go_route_ids)
            for canonical, awin in sorted(all_required):
                if (canonical, awin) in rendered_so_far:
                    continue
                detail = release / "public" / "providers" / canonical / "index.html"
                if detail.is_file():
                    page_html += detail.read_text(errors="replace")
            required = all_required
            forbidden = all_forbidden
        missing_presence = sorted(AFFILIATE_TRACKING_PROVIDER_IDS - present_ids)
        actual = _rendered_affiliate_pairs(surface, page_html, go_route_ids)
        missing_tracking = sorted(f"{canonical}:{awin}" for canonical, awin in required - actual)
        forbidden_tracking = sorted(f"{canonical}:{awin}" for canonical, awin in forbidden & actual)
        if missing_presence:
            failures.append(f"{surface}: required provider presence missing {missing_presence}")
        if missing_tracking:
            failures.append(f"{surface}: required tracked-link pairs missing {missing_tracking}")
        if forbidden_tracking:
            failures.append(f"{surface}: forbidden tracked-link pairs present {forbidden_tracking}")
        per_surface[surface] = {
            "route": route,
            "required_provider_count": len(AFFILIATE_TRACKING_PROVIDER_IDS),
            "provider_presence_count": len(AFFILIATE_TRACKING_PROVIDER_IDS & present_ids),
            "required_tracked_link_count": len(required),
            "tracked_link_count": len(required & actual),
            "missing_provider_ids": missing_presence,
            "missing_tracked_pairs": missing_tracking,
            "forbidden_tracked_pairs": forbidden_tracking,
        }
    return check("affiliate_surface_tracking", not failures,
                 required_awin_item1_provider_count=len(AWIN_ITEM1_IDS),
                 required_awin_item1_calculator_count=len(calculator_required),
                 required_best_terms_route="shedrx:katalys-1516",
                 required_affiliate_provider_count=len(AFFILIATE_TRACKING_PROVIDER_IDS),
                 active_go_route_count=len(go_route_ids),
                 per_surface=per_surface, failures=failures)


def check_provider_link_monetization(release: Path) -> dict[str, Any]:
    """Every approved-provider link is monetized, or explicitly marked with a reason.

    OPERATOR DIRECTIVE 2026-08-19 (verbatim): "make sure that we wire ANY link to
    ANY approved provider on our site with a valid affiliate link so that we make
    sure anytime anyone clicks a link for a provider on our site, regardless of
    where it is, we get paid if they sign up. this should have been the standard
    from the start."

    The build gate already enforces this at build time. It runs here too, on the
    RELEASE ABOUT TO SHIP, because a provider wired next month must be attributed
    everywhere it appears sitewide - not only on its own review page. A deploy
    that would publish an unmarked bare approved-provider link, an untagged
    affiliate link, or a link into our own partner portal does not pass.
    """
    root = release.resolve()
    validator = root / "scripts/validate-approved-provider-link-monetization.mjs"
    tree = root / "public"
    if not validator.is_file():
        return skipped("provider_link_monetization",
                       "release predates scripts/validate-approved-provider-link-monetization.mjs")
    if not tree.is_dir():
        return check("provider_link_monetization", False,
                     failures=[f"built tree missing: {tree}"])
    # A sealed release is root-owned and read-only by design, so the validator's
    # report goes to a scratch dir, never inside the tree being judged.
    scratch = Path(tempfile.mkdtemp(prefix="glp-provider-link-sensor-"))
    out = scratch / "report.json"
    try:
        proc = subprocess.run(
            ["node", str(validator), "--tree", str(tree), "--json", str(out), "--allow-failures"],
            cwd=str(root), capture_output=True, text=True, timeout=300, check=False)
    except Exception as exc:  # noqa: BLE001 - a sensor never crashes the caller
        shutil.rmtree(scratch, ignore_errors=True)
        return check("provider_link_monetization", False,
                     failures=[f"validator did not run: {exc}"])
    try:
        report = json.loads(out.read_text())
    except Exception as exc:  # noqa: BLE001
        return check("provider_link_monetization", False,
                     failures=[f"validator produced no readable report: {exc}",
                               (proc.stderr or proc.stdout or "").strip()[:400]])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    findings = report.get("findings") or []
    by_class: dict[str, int] = {}
    for item in findings:
        by_class[item.get("class", "unknown")] = by_class.get(item.get("class", "unknown"), 0) + 1
    failures = [f"{item.get('class')}: {item.get('file')} -> {item.get('href')}" for item in findings[:40]]
    if len(findings) > 40:
        failures.append(f"...{len(findings) - 40} more")
    counts = report.get("counts") or {}
    return check("provider_link_monetization", not findings,
                 pages_scanned=report.get("files_scanned"),
                 approved_provider_domain_count=report.get("approved_provider_domain_count"),
                 approved_program_count=report.get("approved_program_count"),
                 provider_anchors=counts.get("provider_anchors"),
                 monetized_anchors=counts.get("monetized"),
                 marked_citation_anchors=counts.get("marked_citation"),
                 findings_by_class=by_class,
                 failures=failures)


def check_therapeutic_facet_bindings(release: Path, rows: list[dict[str, Any]],
                                     now: dt.datetime | None = None) -> dict[str, Any]:
    """One evidence projection must bind every wired provider to compare and quiz.

    This is intentionally stricter than presence-only surface coverage. A provider
    can be present on both pages and still be invisible to a therapeutic filter if
    its taxonomy row or rendered tokens were omitted. Stale taxonomy rows are also
    failures: a removed registry provider may not linger as an eligible result.
    """
    failures: list[str] = []
    try:
        registry = load_json(release / "data/program-registry.json")
        money = load_json(release / "data/money-page-specs.json")
        quiz = load_json(release / "data/quiz-config.json")
    except (OSError, ValueError) as exc:
        return check("therapeutic_facet_bindings", False,
                     failures=[f"therapeutic inputs unreadable: {exc}"])
    try:
        option_c_contract = option_c_provider_card_contract(release)
    except (OSError, ValueError):
        option_c_contract = None
    option_c_excluded = set((option_c_contract or {}).get("excluded_program_ids") or [])
    document = registry.get("therapeutic_taxonomy") if isinstance(registry, dict) else None
    if not isinstance(document, dict) or document.get("schema") != "glp-therapeutic-taxonomy-v1":
        return check("therapeutic_facet_bindings", False,
                     failures=["data/program-registry.json has no glp-therapeutic-taxonomy-v1 projection"])

    current_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _live_selector(row):
            continue
        pid = render_id(row)
        if pid in current_by_id:
            failures.append(f"{pid}: duplicate current render identity in registry")
        current_by_id[pid] = row
    taxonomy = document.get("programs") or {}
    if not isinstance(taxonomy, dict):
        taxonomy = {}
        failures.append("therapeutic_taxonomy.programs is not an object")
    current_ids = set(current_by_id)
    taxonomy_ids = set(taxonomy)
    missing = sorted(current_ids - taxonomy_ids)
    stale = sorted(taxonomy_ids - current_ids)
    if missing:
        failures.append(f"current wired providers missing taxonomy: {missing}")
    if stale:
        failures.append(f"stale taxonomy references non-current providers: {stale}")

    authorities = document.get("authorities") or {}
    authority_state: dict[str, Any] = {}
    for name, authority in authorities.items():
        relative = str((authority or {}).get("path") or "")
        expected = str((authority or {}).get("sha256") or "")
        actual = None
        if not relative.startswith("reports/") or not re.fullmatch(r"[a-f0-9]{64}", expected):
            failures.append(f"authority {name}: invalid path/hash declaration")
        else:
            evidence_path = BUSINESS / relative
            if not evidence_path.is_file():
                failures.append(f"authority {name}: evidence file missing at {relative}")
            else:
                actual = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                if actual != expected:
                    failures.append(f"authority {name}: evidence hash mismatch {actual} != {expected}")
        authority_state[name] = {"path": relative, "expected_sha256": expected,
                                 "actual_sha256": actual}

    captured_now = now or dt.datetime.now(dt.timezone.utc)
    today = captured_now.date()
    max_age = document.get("freshness_max_days")
    if not isinstance(max_age, int) or not 1 <= max_age <= 90:
        failures.append(f"invalid taxonomy freshness_max_days: {max_age!r}")
        max_age = 0
    per_program: dict[str, Any] = {}
    for pid in sorted(current_ids & taxonomy_ids):
        row = taxonomy[pid]
        registry_row = current_by_id[pid]
        row_failures: list[str] = []
        if row.get("program_id") != pid or row.get("registry_program_id") != registry_row.get("program_id"):
            row_failures.append("registry/program identity binding mismatch")
        for field, allowed in THERAPEUTIC_ALLOWED.items():
            values = row.get(field)
            if (not isinstance(values, list) or values != sorted(set(values))
                    or any(value not in allowed for value in values)):
                row_failures.append(f"{field} is not a sorted, unique, allowlisted evidence array")
        treatment_state = "verified" if row.get("treatment_type_tags") else "not_verified"
        if row.get("treatment_type_state") != treatment_state:
            row_failures.append("treatment_type_state disagrees with explicit evidence")
        if row.get("normalized_sha256") != _taxonomy_sha256(row):
            row_failures.append("normalized_sha256 mismatch")
        try:
            verified = dt.date.fromisoformat(str(row.get("verified_as_of") or ""))
            age = (today - verified).days
            if age < -1 or age > max_age:
                row_failures.append(f"evidence age {age}d outside 0..{max_age}d policy")
        except ValueError:
            age = None
            row_failures.append("verified_as_of is not ISO date")
        source_urls = row.get("source_urls")
        if (not isinstance(source_urls, list) or not source_urls
                or source_urls != sorted(set(source_urls))
                or any(not str(url).startswith("https://") for url in source_urls)):
            row_failures.append("source_urls are not sorted, unique HTTPS evidence URLs")
        bindings = row.get("evidence_bindings")
        if not isinstance(bindings, list) or not bindings:
            row_failures.append("evidence_bindings missing")
        else:
            for binding in bindings:
                authority = authorities.get(binding.get("authority")) or {}
                if binding.get("authority_sha256") != authority.get("sha256"):
                    row_failures.append("evidence binding authority hash mismatch")
                if binding.get("source_url") not in (source_urls or []):
                    row_failures.append("evidence binding source URL mismatch")
                digest_ok = re.fullmatch(r"[a-f0-9]{64}", str(binding.get("evidence_sha256") or ""))
                capture_ok = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}",
                                          str(binding.get("capture_id") or ""))
                if not digest_ok and not capture_ok:
                    row_failures.append("evidence binding has no hash or capture id")
        link_state = row.get("commercial_link_state") or {}
        if link_state.get("compare") not in ("live", "editorial_link_dark") \
                or link_state.get("quiz") not in ("live", "editorial_link_dark"):
            row_failures.append("commercial link state invalid")
        if row_failures:
            failures.extend(f"{pid}: {message}" for message in row_failures)
        per_program[pid] = {"tokens": _therapeutic_tokens(row),
                            "treatment_types": row.get("treatment_type_tags") or [],
                            "verified_as_of": row.get("verified_as_of"),
                            "evidence_age_days": age,
                            "failures": row_failures}

    def count(field: str, value: str) -> int:
        return sum(value in (row.get(field) or []) for row in taxonomy.values())

    actual_counts = {
        "provider_rows": len(taxonomy),
        "semaglutide": count("molecule_tags", "semaglutide"),
        "tirzepatide": count("molecule_tags", "tirzepatide"),
        "retatrutide": count("molecule_tags", "retatrutide"),
        "wegovy": count("brand_name_tags", "wegovy"),
        "zepbound": count("brand_name_tags", "zepbound"),
        "mounjaro": count("brand_name_tags", "mounjaro"),
        "oral": count("delivery_form_tags", "oral"),
        "injectable": count("delivery_form_tags", "injectable"),
        "compounded": count("treatment_type_tags", "compounded"),
        "brand_name": count("treatment_type_tags", "brand_name"),
        "treatment_type_unknown": sum(not (row.get("treatment_type_tags") or [])
                                      for row in taxonomy.values()),
    }
    if document.get("expected_counts") != actual_counts:
        failures.append("therapeutic expected_counts do not equal the evidence-derived counts")
    if actual_counts["retatrutide"] != 0:
        failures.append("Build 6 contract requires zero explicitly evidenced retatrutide providers")

    # The evidence register is program-granular (29 rows), while shipped lists are
    # merchant-granular (28 rows after declared aliases are consolidated). Merge an
    # alias's verified tags into its canonical merchant; never revive the alias as a
    # second compare/quiz row just to make the taxonomy denominator look larger.
    projected_taxonomy: dict[str, dict[str, Any]] = {}
    projection_sources: dict[str, list[dict[str, Any]]] = {}
    for pid, taxonomy_row in taxonomy.items():
        registry_row = current_by_id.get(pid) or {}
        identity = ((registry_row.get("profiles") or {}).get(PROFILE) or {}).get("canonical_identity") or {}
        canonical_id = str(identity.get("canonical_program_id") or "") \
            if identity.get("role") == "alias" else pid
        if canonical_id not in taxonomy:
            failures.append(f"{pid}: canonical therapeutic projection target {canonical_id!r} missing")
            continue
        projection_sources.setdefault(canonical_id, []).append(taxonomy_row)
    for canonical_id, source_rows in sorted(projection_sources.items()):
        projected = dict(taxonomy[canonical_id])
        for field in THERAPEUTIC_ALLOWED:
            projected[field] = sorted({tag for row in source_rows for tag in (row.get(field) or [])})
        projected["treatment_type_state"] = \
            "verified" if projected["treatment_type_tags"] else "not_verified"
        if len(source_rows) == 1:
            projected["normalized_sha256"] = taxonomy[canonical_id].get("normalized_sha256")
        else:
            projection_basis = json.dumps({
                "canonical_program_id": canonical_id,
                "source_taxonomy_sha256s": sorted(row.get("normalized_sha256") for row in source_rows),
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            projected["normalized_sha256"] = hashlib.sha256(projection_basis.encode()).hexdigest()
        projected_taxonomy[canonical_id] = projected
    projected_ids = set(projected_taxonomy)
    # The taxonomy is the evidence denominator, not an instruction to expose a
    # held editorial row in a conversion pool. Respect the registry's canonical
    # per-surface authority after alias projection (for example TRT Nation stays
    # on compare/review but is deliberately absent from quiz/calculator).
    projected_quiz_ids = {
        pid for pid in projected_ids
        if ((((current_by_id.get(pid) or {}).get("profiles") or {}).get(PROFILE) or {})
            .get("surfaces") or {}).get("quiz") is not False
    }
    dynamic_profiles = _validate_dynamic_profile_projection(
        release, registry, projected_taxonomy, captured_now, failures)
    retatrutide_available = _retatrutide_consumer_available(
        registry, dynamic_profiles, captured_now, failures)
    consumer_filter_options = tuple(
        option for option in THERAPEUTIC_REQUIRED_OPTIONS
        if option != "retatrutide" or retatrutide_available)
    consumer_quiz_options = tuple(
        option for option in THERAPEUTIC_QUIZ_OPTIONS
        if option != "retatrutide" or retatrutide_available)

    required_paths = document.get("required_surface_paths") or []
    page_by_path = {page.get("path"): page for page in money.get("pages") or []}
    raw_source_ids = {str(row.get("program_id"))
                      for page in money.get("pages") or [] for row in page.get("rows") or []}
    raw_source_ids.update(str(row.get("program_id")) for row in quiz.get("candidates") or [])
    held_proofs = _receipted_editorial_holds(release, rows, raw_source_ids)
    held_ids = {pid for pid, proof in held_proofs.items() if proof["exempt"]}
    failures.extend(f"{pid}: receipted editorial HOLD proof failed: {reason}"
                    for pid, proof in held_proofs.items() for reason in proof["failures"])
    source_surface_counts: dict[str, int] = {}
    projected_source_surface_counts: dict[str, int] = {}
    for route in required_paths:
        page = page_by_path.get(route)
        if not page:
            failures.append(f"{route}: required compare source missing")
            continue
        source_rows = {str(row.get("program_id")): row for row in page.get("rows") or []}
        source_surface_counts[route] = len(source_rows)
        if len(source_rows) != len(page.get("rows") or []):
            failures.append(f"{route}: duplicate compare source identity")
        projected_source_ids = _editorial_source_projection(source_rows, held_ids, route, failures)
        projected_source_surface_counts[route] = len(projected_source_ids)
        if projected_source_ids != taxonomy_ids:
            failures.append(f"{route}: compare source membership does not equal taxonomy")
        for pid in sorted(set(source_rows) & taxonomy_ids):
            expected_state = (taxonomy[pid].get("commercial_link_state") or {}).get("compare")
            if _commercial_link_state(source_rows[pid]) != expected_state:
                failures.append(f"{route}:{pid}: compare commercial link state drift")

    quiz_candidates = {str(candidate.get("program_id")): candidate
                       for candidate in quiz.get("candidates") or []}
    if len(quiz_candidates) != len(quiz.get("candidates") or []):
        failures.append("duplicate quiz source identity")
    projected_quiz_source_ids = _editorial_source_projection(
        quiz_candidates, held_ids, "quiz", failures)
    if projected_quiz_source_ids != taxonomy_ids:
        failures.append("quiz source membership does not equal taxonomy")
    question = next((item for item in quiz.get("questions") or []
                     if item.get("id") == "medication_preference"), None)
    if not question or question.get("label") != "Do you already know which medication you want?":
        failures.append("quiz medication preference question missing or not contract-exact")
    elif tuple(item.get("value") for item in question.get("options") or []) != THERAPEUTIC_QUIZ_OPTIONS:
        failures.append("quiz medication preference options do not match the Build 6b contract")

    public_taxonomy_path = release / "public/data/therapeutic-taxonomy.json"
    try:
        if load_json(public_taxonomy_path) != document:
            failures.append("generated public therapeutic taxonomy differs from registry source")
    except (OSError, ValueError) as exc:
        failures.append(f"generated public therapeutic taxonomy unreadable: {exc}")

    rendered_surface_counts: dict[str, int] = {}
    # 2026-08-27 redesign: a taxonomy-declared surface path may have been retired
    # into a successor route. The declaration lives in a machine-sealed section,
    # so the sensor follows the release's own redirect (or query-aware Function
    # successor) instead of demanding the retired file. An uncovered path still
    # fails exactly as before.
    redirect_map: dict[str, str] = {}
    redirects_path = release / "public" / "_redirects"
    if redirects_path.exists():
        for line in redirects_path.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].startswith("/"):
                redirect_map[parts[0].rstrip("/") + "/"] = parts[1].rstrip("/") + "/"
    for route in required_paths:
        page_path = release / "public" / route.strip("/") / "index.html"
        if not page_path.is_file():
            successor = redirect_map.get(route.rstrip("/") + "/")
            if not successor and (release / "functions" / route.strip("/") / "index.js").is_file():
                successor = "/providers/"
            if successor:
                page_path = release / "public" / successor.strip("/") / "index.html"
        try:
            body = page_path.read_text(errors="replace")
        except OSError as exc:
            failures.append(f"{route}: built compare surface unreadable: {exc}")
            continue
        if 'data-therapeutic-facets="true"' not in body and "data-provider-filters" in body:
            # 2026-08-27 successor grammar: the redesigned hub filters via its
            # own mount (data-provider-filters) with per-card data-drugs tokens
            # derived at build from the same registry the public-taxonomy
            # equality check above already pins. Membership must still equal the
            # canonical projection (cards + governed record links); per-row
            # taxonomy-sha pinning is superseded by single-source derivation.
            cards = dict(re.findall(r'<article\b[^>]*data-program="([a-z0-9-]+)"[^>]*(data-drugs="[^"]*")', body))
            members = set(cards) | set(re.findall(r'data-provider-record="([a-z0-9-]+)"', body))
            rendered_surface_counts[route] = len(members)
            # Option C deliberately removes evidence-ineligible providers from
            # the public provider-card pool while retaining their taxonomy and
            # editorial records. The contract is exact and independently
            # validated, so only its named exclusions leave the redesigned hub
            # projection; an undeclared omission still fails closed.
            expected_rendered_ids = projected_ids - option_c_excluded
            if not expected_rendered_ids <= members:
                failures.append(f"{route}: built compare membership does not cover canonical taxonomy projection")
            for pid, drugs_attr in cards.items():
                if pid in expected_rendered_ids and 'data-drugs=""' == drugs_attr:
                    failures.append(f"{route}:{pid}: successor facet tokens empty")
            continue
        if 'data-therapeutic-facets="true"' not in body:
            failures.append(f"{route}: therapeutic filter mount missing")
        if dynamic_profiles is not None:
            table_match = re.search(r'<table\b[^>]*data-therapeutic-facets="true"[^>]*>', body, re.I)
            table_attrs = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"',
                                          table_match.group(0) if table_match else ""))
            if table_attrs.get("data-provider-dynamic-profile-schema") != \
                    "glp-provider-dynamic-profiles-v1":
                failures.append(f"{route}: provider dynamic profile mount binding missing")
            if table_attrs.get("data-consumer-authority-schema") != \
                    "glp-consumer-therapeutic-authority-v1":
                failures.append(f"{route}: consumer therapeutic authority mount binding missing")
            if table_attrs.get("data-canonical-provider-ids", "").split(",") != \
                    sorted(dynamic_profiles):
                failures.append(f"{route}: suggestion/canonical provider binding missing or stale")
            if table_attrs.get("data-consumer-therapeutics", "").split(",") != \
                    list(consumer_filter_options):
                failures.append(f"{route}: consumer medication authority binding missing or stale")
        rendered_rows: dict[str, dict[str, str]] = {}
        for tag in re.findall(r"<tr\b[^>]*>", body, re.I):
            attrs = dict(re.findall(r"([a-zA-Z0-9_-]+)=\"([^\"]*)\"", tag))
            if attrs.get("data-program"):
                rendered_rows[attrs["data-program"]] = attrs
        rendered_surface_counts[route] = len(rendered_rows)
        if set(rendered_rows) != projected_ids:
            failures.append(f"{route}: built compare membership does not equal canonical taxonomy projection")
        for pid in sorted(set(rendered_rows) & projected_ids):
            attrs = rendered_rows[pid]
            dynamic_profile = (dynamic_profiles or {}).get(pid) \
                if dynamic_profiles is not None else None
            if dynamic_profiles is not None and not isinstance(dynamic_profile, dict):
                failures.append(f"{route}:{pid}: provider dynamic profile missing at render binding")
                continue
            projection = dynamic_profile.get("taxonomy") \
                if dynamic_profiles is not None else projected_taxonomy[pid]
            if not isinstance(projection, dict):
                failures.append(f"{route}:{pid}: provider dynamic taxonomy missing at render binding")
                continue
            if sorted(filter(None, attrs.get("data-therapeutic", "").split())) != _therapeutic_tokens(projection):
                failures.append(f"{route}:{pid}: compare filter facet tokens missing or stale")
            if sorted(filter(None, attrs.get("data-treatment", "").split())) != projection.get("treatment_type_tags", []):
                failures.append(f"{route}:{pid}: compare treatment facet tokens missing or stale")
            expected_sha = dynamic_profile.get("normalized_sha256") \
                if dynamic_profiles is not None else projected_taxonomy[pid].get("normalized_sha256")
            if attrs.get("data-taxonomy-sha") != expected_sha:
                failures.append(f"{route}:{pid}: compare taxonomy hash binding missing or stale")

    site_js_path = release / "public/assets/site.js"
    try:
        site_js = site_js_path.read_text(errors="replace")
        if dynamic_profiles is None:
            for option in THERAPEUTIC_REQUIRED_OPTIONS:
                if f"['{option}'," not in site_js:
                    failures.append(f"compare filter control missing option: {option}")
        elif not retatrutide_available:
            required_copy = (
                "Retatrutide is not available as a consumer provider filter",
                RETATRUTIDE_AUTHORITIES["fda"], RETATRUTIDE_AUTHORITIES["lilly"],
            )
            for text in required_copy:
                if text not in site_js:
                    failures.append("retatrutide unavailable guard or authority link missing from runtime")
    except OSError as exc:
        failures.append(f"built therapeutic filter runtime unreadable: {exc}")

    rendered_quiz: dict[str, Any] = {}
    quiz_path = release / "public/quiz/provider-fit/index.html"
    try:
        quiz_html = quiz_path.read_text(errors="replace")
        match = re.search(r'<script[^>]*id="candidate-data"[^>]*>(.*?)</script>', quiz_html, re.S)
        if not match:
            failures.append("built quiz candidate-data binding missing")
        else:
            rendered_quiz = {str(item.get("program_id")): item
                             for item in json.loads(html.unescape(match.group(1)))}
            if set(rendered_quiz) != projected_quiz_ids:
                failures.append("built quiz membership does not equal canonical taxonomy projection")
            for pid in sorted(set(rendered_quiz) & projected_quiz_ids):
                candidate = rendered_quiz[pid]
                dynamic_profile = (dynamic_profiles or {}).get(pid) \
                    if dynamic_profiles is not None else None
                if dynamic_profiles is not None and not isinstance(dynamic_profile, dict):
                    failures.append(f"{pid}: provider dynamic profile missing at quiz binding")
                    continue
                projection = dynamic_profile.get("taxonomy") \
                    if dynamic_profiles is not None else projected_taxonomy[pid]
                if not isinstance(projection, dict):
                    failures.append(f"{pid}: provider dynamic taxonomy missing at quiz binding")
                    continue
                if sorted(candidate.get("therapeutic_tags") or []) != _therapeutic_tokens(projection):
                    failures.append(f"{pid}: quiz eligibility therapeutic tags missing or stale")
                expected_state = (projected_taxonomy[pid].get("commercial_link_state") or {}).get("quiz")
                if candidate.get("commercial_link_state") != expected_state:
                    failures.append(f"{pid}: quiz commercial link state drift")
        phase4_stage = 'data-value="SEMAGLUTIDE"' in quiz_html and 'data-value="NOT_SURE"' in quiz_html
        if phase4_stage:
            # 2026-08-27 successor grammar: the phase-4 quiz asks the treatment
            # question with uppercase data-value tokens; WEGOVY_PILL is the oral
            # route and compounded SEMAGLUTIDE/TIRZEPATIDE are the injectable
            # routes, so the legacy oral/injectable options map onto tokens
            # instead of disappearing. An unavailable therapeutic still must not
            # render.
            successor_token = {
                "semaglutide": ["SEMAGLUTIDE"], "tirzepatide": ["TIRZEPATIDE"],
                "wegovy": ["WEGOVY"], "zepbound": ["ZEPBOUND"],
                "mounjaro": ["MOUNJARO"], "ozempic": ["OZEMPIC"],
                "oral": ["WEGOVY_PILL"], "injectable": ["SEMAGLUTIDE", "TIRZEPATIDE"],
                "not_sure": ["NOT_SURE"], "retatrutide": ["RETATRUTIDE"],
            }
            for option in consumer_quiz_options:
                tokens = successor_token.get(option, [option.upper()])
                if not all(f'data-value="{token}"' in quiz_html for token in tokens):
                    failures.append(f"built quiz medication option missing: {option}")
            for option in set(THERAPEUTIC_QUIZ_OPTIONS) - set(consumer_quiz_options):
                for token in successor_token.get(option, [option.upper()]):
                    shared = any(token in successor_token.get(live_opt, [live_opt.upper()])
                                 for live_opt in consumer_quiz_options)
                    if not shared and f'data-value="{token}"' in quiz_html:
                        failures.append(f"built quiz renders unavailable medication option: {option}")
        else:
            if "Do you already know which medication you want?" not in quiz_html:
                failures.append("built quiz does not render the contract-exact medication question")
            for option in consumer_quiz_options:
                if f'data-question="medication_preference" data-value="{option}"' not in quiz_html:
                    failures.append(f"built quiz medication option missing: {option}")
            for option in set(THERAPEUTIC_QUIZ_OPTIONS) - set(consumer_quiz_options):
                if f'data-question="medication_preference" data-value="{option}"' in quiz_html:
                    failures.append(f"built quiz renders unavailable medication option: {option}")
    except (OSError, ValueError) as exc:
        failures.append(f"built quiz therapeutic binding unreadable: {exc}")

    return check("therapeutic_facet_bindings", not failures,
                 current_registry_programs=len(current_ids), taxonomy_programs=len(taxonomy_ids),
                 canonical_projection_programs=len(projected_ids),
                 missing_taxonomy=missing, stale_taxonomy=stale,
                 expected_counts=document.get("expected_counts"), actual_counts=actual_counts,
                 source_surface_counts=source_surface_counts,
                 projected_source_surface_counts=projected_source_surface_counts,
                 quiz_source_candidates=len(quiz_candidates),
                 projected_quiz_source_candidates=len(projected_quiz_source_ids),
                 receipted_editorial_held_ids=sorted(held_ids),
                 rendered_surface_counts=rendered_surface_counts,
                 rendered_quiz_candidates=len(rendered_quiz), authorities=authority_state,
                 dynamic_profile_schema=(registry.get("provider_dynamic_profiles") or {}).get("schema"),
                 dynamic_provider_profiles=(len(dynamic_profiles)
                                            if dynamic_profiles is not None else None),
                 retatrutide_consumer_available=retatrutide_available,
                 consumer_filter_options=list(consumer_filter_options),
                 consumer_quiz_options=list(consumer_quiz_options),
                 programs=per_program, failures=failures)


def check_count_copy(release: Path, rows: list[dict[str, Any]], site: str,
                     offline: bool) -> dict[str, Any]:
    """Every provider count a reader can see equals the canonical live count.

    Render-grep, not a data check: the operator's complaint was that the compare
    page SAID "13 providers" while 29 were live. Reading data files would not have
    caught it, because the sentence lived in prose. This reads the shipped bytes.

    Operator ruling 2026-08-29 is intentionally absolute: every variable
    provider count equals the live count. A smaller evidence subset may be
    stated as "32 have a price row" only after the page first states the
    canonical "37 providers" universe; it may not relabel 32 as "providers".
    """
    expected = len(live_ids(rows))
    failures: list[str] = []
    seen: dict[str, Any] = {}

    def scan(label: str, html: str) -> None:
        text = _visible_text(html)
        found = []
        lazy_cards = len(re.findall(r"\bdata-provider-lazy-index=", html, re.I))

        def record(match: re.Match[str], value: int) -> None:
            phrase = match.group(0)
            if ("more providers" in phrase.lower()
                    and "data-provider-load-more" in html):
                expected_batch = min(PROVIDER_LOAD_MORE_BATCH, lazy_cards)
                found.append({
                    "phrase": phrase,
                    "value": value,
                    "scope": "lazy_provider_cards",
                    "expected": expected_batch,
                    "lazy_cards_remaining": lazy_cards,
                })
                if value != expected_batch:
                    failures.append(
                        f"{label}: says {phrase!r} but the next batch is "
                        f"{expected_batch} of {lazy_cards} lazy provider cards"
                    )
                return
            found.append({
                "phrase": phrase,
                "value": value,
                "scope": "canonical_live_programs",
                "expected": expected,
            })
            if value == expected:
                return
            failures.append(
                f"{label}: says {phrase!r} but {expected} providers are live"
            )

        for match in COUNT_COPY.finditer(text):
            record(match, int(match.group(1)))
        for match in WORD_COPY.finditer(text):
            record(match, NUMBER_WORDS[match.group(1).lower()])
        if "{{" in text and "}}" in text:
            failures.append(f"{label}: shipped an unresolved build token to a reader")
        seen[label] = found

    for route in COUNT_COPY_ROUTES:
        path = release / "public" / route
        if path.is_file():
            scan(f"built:/{route}", path.read_text(errors="replace"))
    if not offline:
        for route in COUNT_COPY_ROUTES:
            url = site.rstrip("/") + "/" + ("" if route == "index.html" else route.replace("index.html", ""))
            try:
                proc = subprocess.run(["curl", "-fsSL", "--max-time", "30", url],
                                      text=True, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"live:{url}: not fetched ({exc})")
                continue
            if proc.returncode != 0:
                failures.append(f"live:{url}: HTTP fetch failed rc={proc.returncode}")
                continue
            scan(f"live:{url}", proc.stdout)
    return check("count_copy", not failures, expected_live_count=expected,
                 routes_scanned=len(seen), phrases=seen, failures=failures)


#: A research field the OPERATOR has ruled out of scope. Recorded, visible in
#: the receipt, and never merged into the content ledger - the site does not make
#: the claim. Must carry a basis naming the ruling, so this can never be a quiet
#: way to drop a field the research simply failed to find.
#:
#: The ruling is read from CONFIG at check time rather than stamped into the
#: packets. 37 ledger rows bind their program-research packet by sha256, so
#: rewriting those bytes would invalidate the bindings - the identical defect
#: class as the therapeutic-authority hash drift this same deploy hit. A packet
#: MAY also carry the status inline (future packets written with it); either
#: source satisfies the check, and both require a basis.
NOT_IN_SCOPE_STATUS = "not_in_scope"
RESEARCH_FIELD_SCOPE_CONFIG = BUSINESS / "config/affiliate-sites/research-field-scope.json"


def research_field_scope_rulings(config_path: Path | None = None) -> dict[str, str]:
    """Operator field-scope rulings as {field_name: basis}. Absent config = none."""
    path = Path(config_path or RESEARCH_FIELD_SCOPE_CONFIG)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if document.get("schema") != "glp.research-field-scope.v1":
        return {}
    rulings: dict[str, str] = {}
    for name, ruling in (document.get("out_of_scope_fields") or {}).items():
        basis = str((ruling or {}).get("basis") or "").strip()
        if basis:
            rulings[str(name)] = basis
    return rulings


# Explicit operator dispositions, not a stale-packet waiver. Additional held
# programs require their own reviewed immutable receipt registration.
RECEIPTED_RESEARCH_HOLDS = {
    "telos-rx": {
        "receipt_path": "data/telos-research-hold-receipt.json",
        "receipt_sha256": "f18426e9dea271a1c8ae5b234324ddbb57c3c524e61c1494dd8c281174c9c585",
        "ack_sha256": "3eabb6f43dbd2610f313eb1b1220e5aa5d59ae36426b5dd6e31e13db4d710b8e",
        "row_sha256": "323acb673fb7628c50229fce9adddfa0a4ae0d30c66944e84d85103a6f0566e8",
    }
}


def receipted_held_link_dark(release: Path, pid: str) -> dict[str, Any]:
    """Read-only proof. Never grants routing, research, or promotion authority.

    A commercial HOLD must exist independently of missing promotion booleans.
    Both current inputs AND generated/runtime/public bytes must be link-dark.
    Historical research/issued-link evidence may remain in source contracts.
    """
    from urllib.parse import unquote
    result: dict[str, Any] = {"exempt": False, "failures": []}
    failures = result["failures"]
    expected = RECEIPTED_RESEARCH_HOLDS.get(pid)
    if expected is None:
        failures.append("no_registered_receipted_hold")
        return result
    try:
        def require(ok: bool, code: str) -> None:
            if not ok:
                raise ValueError(code)
        raw = (release / expected["receipt_path"]).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected["receipt_sha256"], "hold_receipt_hash_invalid")
        receipt = json.loads(raw)
        require(receipt["schema"] == "glp-receipted-research-hold-v1"
                and receipt["program_id"] == pid and receipt["disposition"] == "HOLD"
                and receipt["promotion_authorized"] is False and receipt["live_link_allowed"] is False
                and receipt["research_intake_authorized"] is False, "hold_receipt_contract_invalid")
        for field, digest in [("operator_ack", "ack_sha256"), ("row_readback", "row_sha256")]:
            ref = receipt[field]
            require(ref["sha256"] == expected[digest]
                    and hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() == expected[digest],
                    field + "_hash_invalid")
        source = json.loads(Path(receipt["row_readback"]["path"]).read_text())
        evidence = [r for r in source["rows"] if r["event"]["sheet_row"] == receipt["sheet_row"]]
        require(len(evidence) == 1 and evidence[0]["approval_claim"] is False, "hold_row_identity_invalid")
        event = evidence[0]["event"]
        require(event["seo_slug"] == receipt["sheet_seo_slug"] and event["network"] == receipt["network"]
                and event["row_fingerprint"] == receipt["row_readback"]["row_fingerprint"]
                and event["status"] == "paused_network_program_paused"
                and event["link_qa_status"] == receipt["link_disposition"] == "link_retired_network_program_paused"
                and event["wired_live"] is False and event["wire_in_ready"] is False, "hold_row_state_invalid")
        rows = registry_rows(release)
        exact = [r for r in rows if r.get("program_id") == pid]
        aliases = [r for r in rows if r.get("program_id") == receipt["sheet_seo_slug"]]
        require(len(exact) == len(aliases) == 1, "hold_registry_identity_invalid")
        canonical, alias = exact[0], aliases[0]
        for row, role in [(canonical, "canonical"), (alias, "alias")]:
            profile = _profile(row)
            identity = profile.get("canonical_identity") or {}
            require(identity.get("role") == role and identity.get("canonical_program_id") == pid
                    and identity.get("identity_key") == identity_key(canonical.get("display_name", "")), "hold_alias_invalid")
            require(row.get("research_hold_receipt") == {"path": expected["receipt_path"], "sha256": expected["receipt_sha256"]}, "hold_registry_receipt_invalid")
            require(row.get("commercial_state") == "held" and profile.get("wired") is False
                    and row.get("go", {}).get("entry_state") == "held", "hold_registry_activation_present")
            require(all(profile.get("surfaces", {}).get(s) is False for s in ("compare", "quiz", "calculator", "go")), "hold_conversion_surface_present")
        require(all(v is False for v in _profile(alias)["surfaces"].values()), "hold_alias_surface_present")
        require(canonical.get("review_path") == f"/reviews/{pid}/" and canonical.get("go", {}).get("route") == f"/go/{pid}", "hold_route_identity_invalid")
        def unique(name: str, key: str, collection: str = "programs") -> dict[str, Any]:
            items = load_json(release / name)[collection]
            matches = [r for r in items if r.get(key) == pid]
            require(len(matches) == 1, "hold_source_identity_invalid:" + name)
            return matches[0]
        for name in ("data/program-content-ledger.json", "data/program-page-eligibility.json", "src/program-page-eligibility.json"):
            row = unique(name, "program_id")
            require(row.get("live_link_allowed") is False and row.get("promotion_authorized") is False
                    and row.get("go_route_wired") is False, "hold_source_activation_present:" + name)
        contract = unique("data/provider-page-contracts.json", "program_id", "providers")
        net, card = contract["network_contract"], contract["row_contract"]
        require(net.get("network") == receipt["network"] and net.get("immutable_offer_id") == receipt["network_program_id"], "hold_network_identity_invalid")
        require(net.get("relationship_state") == "paused_network_program_paused" and bool(net.get("activation_blocker"))
                and net.get("promotion_authorized_in_this_artifact") is False
                and card.get("live_link_allowed") is False and card.get("promotion_authorized") is False
                and not card.get("affiliate_href") and contract["publication_state"].get("affiliate_activation_candidate_after_gate") is False,
                "hold_contract_activation_present")
        retired_url = net["tracking_url"]
        integration = unique("data/provider-fastpath-contract.json", "program_id", "providers")
        require(integration.get("route_state") == "READY_EDITORIAL_LINK_DARK"
                and integration.get("affiliate_cta_state") == "HOLD_NETWORK_PROGRAM_PAUSED", "hold_fastpath_activation_present")
        promotion = load_json(release / "data/go-runtime-promotion-authority.json")["programs"].get(pid, {})
        require(promotion.get("status") == "WITHHELD" and promotion.get("promotion_authorized") is False, "hold_promotion_present")
        for page in load_json(release / "data/money-page-specs.json")["pages"]:
            for row in page.get("rows", []):
                if row.get("program_id") == pid:
                    require(row.get("live_link_allowed") is False and row.get("promotion_authorized") is False
                            and not row.get("affiliate_href"), "hold_direct_card_present")
        selection = load_json(release / "data/go-selection-receipt.json")
        require(not any(r.get("program_id") in (pid, receipt["sheet_seo_slug"]) and r.get("disposition") != "HOLD"
                        for r in selection["programs"]), "hold_selection_activation_present")
        manifest = load_json(release / "artifacts/ops/worker-manifest.staging.json")
        require(manifest.get("schema") == "affiliate-worker-staging-v3-registry" and isinstance(manifest.get("programs"), list), "hold_manifest_invalid")
        for row in manifest["programs"]:
            if row.get("program_id") in (pid, receipt["sheet_seo_slug"]):
                require(row.get("live_link_allowed") is False and row.get("promotion_authorized") is False
                        and row.get("go_route_wired") is False and not row.get("tracking"), "hold_generated_manifest_not_dark")
        # Scan executable and reader-visible bytes, including JS/JSON inventories;
        # not only anchors, so alternate escaping/data-driven CTAs cannot hide.
        public = release / "public"
        require(public.is_dir() and any(public.rglob("*.html")), "hold_public_output_missing")
        paths = [p for p in public.rglob("*") if p.is_file() and p.suffix.lower() in (".html", ".js", ".json")]
        runtime = release / "functions/go/program-manifest.js"
        require(runtime.is_file(), "hold_runtime_manifest_missing")
        paths.append(runtime)
        held_routes = "|".join(re.escape(x) for x in (pid, receipt["sheet_seo_slug"]))
        for p in paths:
            text = p.read_text(encoding="utf-8").replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
            for _ in range(3):
                text = html.unescape(unquote(text))
            if retired_url in text or re.search(r"(?:/go/(?:" + held_routes + r")(?:[/?#\s\"'<]|$)|offer_id=1612(?:[&#\s\"'<]|$))", text):
                failures.append("hold_public_tracking_present:" + str(p.relative_to(release)))
        result.update(receipt_sha256=expected["receipt_sha256"], commercial_disposition="HOLD",
                      research_intake_authorized=False, promotion_authorized=False, public_files_checked=len(paths))
        result["exempt"] = not failures
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        failures.append(str(exc))
    return result


def _receipted_editorial_holds(release: Path, rows: list[dict[str, Any]],
                               source_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Canonical proof set, never a generic inactive/false-flag exemption.

    Raw-only registered identities are checked too: deleting their registry row
    cannot turn a malformed HOLD into a quiet source-membership subtraction.
    Alias identities are NOT extra editorial corpus exemptions.
    """
    present = source_ids | {render_id(row) for row in rows}
    return {pid: receipted_held_link_dark(release, pid)
            for pid in sorted(present & set(RECEIPTED_RESEARCH_HOLDS))}


def _editorial_source_projection(source_rows: dict[str, dict[str, Any]],
                                  held_ids: set[str], surface: str,
                                  failures: list[str]) -> set[str]:
    """Subtract only authenticated link-dark editorial rows from a raw corpus.

    A retained held_route is historical source data, not an executable route.
    No output membership, taxonomy or dynamic-profile denominator is reduced.
    """
    removable: set[str] = set()
    for pid in sorted(set(source_rows) & held_ids):
        row = source_rows[pid]
        if (row.get("live_link_allowed") is not False
                or row.get("promotion_authorized") is not False
                or any(row.get(key) for key in ("affiliate_href", "href", "route", "go_route"))):
            failures.append(f"{surface}:{pid}: held editorial source is not link-dark")
        else:
            removable.add(pid)
    return set(source_rows) - removable


def check_research_binding(live: list[str], ledger: dict[str, dict[str, Any]],
                           index: dict[str, dict[str, Any]],
                           excused: dict[str, str] | None = None,
                           release: Path | None = None) -> dict[str, Any]:
    failures: list[str] = []
    per_program: dict[str, Any] = {}
    open_work: dict[str, str] = {}
    scope_rulings = research_field_scope_rulings()
    held_proofs: dict[str, Any] = {}
    if release is not None:
        for row in registry_rows(release):
            pid = render_id(row)
            if not is_alias_row(row) and pid in RECEIPTED_RESEARCH_HOLDS:
                held_proofs[pid] = receipted_held_link_dark(release, pid)
    for pid in sorted(set(live) | set(held_proofs)):
        if pid in held_proofs:
            proof = held_proofs[pid]
            if proof["exempt"]:
                per_program[pid] = {"artifact": None, "research_binding_disposition": "EXEMPT_RECEIPTED_HELD_LINK_DARK", "hold_proof": proof}
                continue
            failures.extend(f"{pid}: receipted HOLD not link-dark: {reason}" for reason in proof["failures"])
        if pid in (excused or {}) and pid not in held_proofs:
            # Live, monetized, and honestly not yet a tracked program. Reported as
            # open work with its recorded reason rather than dropped or waved through.
            open_work[pid] = (excused or {})[pid]
            per_program[pid] = {"artifact": None, "outside_program_register": True}
            continue
        entry = ledger.get(pid)
        artifact = index.get(pid)
        if artifact is None:
            failures.append(f"{pid}: no research artifact on disk")
            per_program[pid] = {"artifact": None}
            continue
        age = _age_days(artifact["captured_at"])
        record: dict[str, Any] = {"artifact": artifact["path"],
                                  "captured_at": artifact["captured_at"],
                                  "age_days": None if age is None else round(age, 2)}
        if age is None:
            failures.append(f"{pid}: research artifact carries no parseable capture date")
        elif age > RESEARCH_MAX_AGE_DAYS:
            failures.append(f"{pid}: research artifact is {age:.0f} days old "
                            f"(max {RESEARCH_MAX_AGE_DAYS})")
        if entry is None:
            failures.append(f"{pid}: research exists but the program has no content-ledger entry "
                            "(research never merged)")
            per_program[pid] = record
            continue
        unmerged: list[str] = []
        merged: list[str] = []
        not_in_scope: list[str] = []
        for name, field in (artifact["doc"].get("fields") or {}).items():
            if name not in LEDGER_ALIASES or not isinstance(field, dict):
                continue
            # A field the operator has ruled OUT OF SCOPE is researched and
            # recorded, but the site does not carry the claim, so there is nothing
            # to merge and demanding it would be demanding a certification the
            # operator has ruled we do not pursue (LegitScript, 2026-08-27/08-31).
            # This is NOT a way to wave a field through: the status must name its
            # basis, and a bare "not_in_scope" with no basis FAILS.
            if name in scope_rulings:
                not_in_scope.append(name)
                continue
            if str(field.get("scope_status") or "") == NOT_IN_SCOPE_STATUS:
                if not str(field.get("scope_basis") or "").strip():
                    failures.append(f"{pid}: field {name!r} claims {NOT_IN_SCOPE_STATUS} "
                                    "without a recorded scope_basis")
                else:
                    not_in_scope.append(name)
                continue
            value = field.get("value")
            if value is None or EMPTYISH.fullmatch(str(value)):
                continue  # researched and honestly absent — nothing to merge
            (merged if _ledger_value(entry, name) is not None else unmerged).append(name)
        record.update({"merged_fields": sorted(merged), "unmerged_fields": sorted(unmerged),
                       "not_in_scope_fields": sorted(not_in_scope)})
        if unmerged:
            failures.append(f"{pid}: research fields present on disk but missing from the "
                            f"content ledger: {sorted(unmerged)}")
        elif not merged and not not_in_scope:
            failures.append(f"{pid}: content-ledger entry carries no researched values")
        per_program[pid] = record
    return check("research_binding", not failures, open_work_outside_register=open_work,
                 programs=per_program, failures=failures,
                 fields_ruled_out_of_scope=sorted(scope_rulings))


def check_page_facts(release: Path, live: list[str],
                     index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = release / "data/provider-page-contracts.json"
    if not path.is_file():
        return check("page_fact_consistency", False,
                     failures=[f"provider-page-contracts.json missing at {path}"])
    raw = load_json(path)
    providers = raw.get("providers") if isinstance(raw, dict) else raw
    if isinstance(providers, dict):
        providers = [{"program_id": k, **v} for k, v in providers.items()]
    failures: list[str] = []
    compared: dict[str, Any] = {}
    live_set = set(live)
    for provider in providers or []:
        pid = str(provider.get("program_id"))
        if pid not in live_set:
            continue
        artifact = index.get(pid)
        if artifact is None:
            failures.append(f"{pid}: page contract exists but no research artifact to check it "
                            "against")
            continue
        copy = provider.get("page_copy") or {}
        detail: dict[str, Any] = {"artifact": artifact["path"], "fields": {}}
        for page_field, research_fields in PAGE_FACT_MAP.items():
            page_text = copy.get(page_field)
            if not isinstance(page_text, str) or not page_text.strip():
                continue
            page_values = value_tokens(page_text)
            if not page_values:
                continue
            attested = value_tokens(research_text(artifact["doc"], research_fields))
            unattested = sorted(page_values - attested)
            detail["fields"][page_field] = {
                "page_tokens": sorted(page_values),
                "research_fields": list(research_fields),
                "research_tokens": sorted(attested),
                "unattested": unattested,
            }
            if unattested:
                failures.append(
                    f"{pid}: page field 'page_copy.{page_field}' asserts {unattested} which the "
                    f"research artifact's {'/'.join(research_fields)} does not attest "
                    f"(research says {sorted(attested) or 'nothing comparable'})")
        compared[pid] = detail
    return check("page_fact_consistency", not failures, programs=compared, failures=failures)


def check_identity(live: list[str], ledger: dict[str, Any], elig: list[str],
                   rows: list[dict[str, Any]], excused: dict[str, str] | None = None) -> dict[str, Any]:
    all_registry = {str(r.get("program_id")) for r in rows} | {render_id(r) for r in rows}
    live_set, ledger_set, elig_set = set(live), set(ledger), set(elig)
    # Eligibility is keyed by registry id; live/ledger by render identity. Accept
    # either spelling so the two-identity system is not reported as drift.
    slug_of = {str(r.get("program_id")): render_id(r) for r in rows}
    elig_set = elig_set | {slug_of.get(pid, pid) for pid in elig_set}
    # A live route may sit outside the program register only with a recorded
    # reason; an unexplained one is still a hard failure.
    excused_ids = set()
    for pid, _reason in (excused or {}).items():
        excused_ids.add(pid)
        excused_ids.add(slug_of.get(pid, pid))
    failures: list[str] = []
    drift = {
        "live_not_in_ledger": sorted(live_set - ledger_set - excused_ids),
        "live_not_in_eligibility": sorted(live_set - elig_set - excused_ids),
        "ledger_not_in_registry": sorted(ledger_set - all_registry),
        "eligibility_not_in_registry": sorted(elig_set - all_registry),
    }
    for label, ids in drift.items():
        if ids:
            failures.append(f"identity drift (G7) {label}: {ids}")
    return check("identity_agreement", not failures,
                 counts={"registry_live": len(live_set), "ledger": len(ledger_set),
                         "eligibility": len(elig_set), "registry_total": len(all_registry),
                         "live_outside_register_with_written_reason": len(excused or {})},
                 drift=drift, outside_register_reasons=excused or {}, failures=failures)


def check_link_graph(release: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    public = release / "public"
    if not public.is_dir():
        return check("link_graph_floor", False,
                     failures=[f"built public/ tree missing at {public}"])
    pages: dict[str, str] = {}
    for path in public.rglob("*.html"):
        try:
            pages[str(path.relative_to(public))] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    failures: list[str] = []
    inbound: dict[str, Any] = {}
    for row in rows:
        if str(row.get("commercial_state") or "").lower() != "live":
            continue
        pid = str(row.get("program_id"))
        review = str(row.get("review_path") or "").strip()
        if not review:
            failures.append(f"{pid}: live program has no review_path")
            continue
        own = review.strip("/") + "/index.html"
        needle = re.compile(r'href="(?:' + re.escape(SITE) + r')?' + re.escape(review.rstrip("/")) +
                            r'/?(?:[?#][^"]*)?"')
        sources = sorted(name for name, html in pages.items()
                         if name != own and needle.search(html))
        inbound[pid] = {"review_path": review, "inbound": len(sources),
                        "sources": sources[:8]}
        # SHED-301 (2026-08-06): a declared alias row's review route is a
        # permanent redirect to its canonical record. It is exempt from the
        # inbound floor — the canonical carries the link equity — and must
        # instead have ZERO inbound links, or readers are being routed through
        # a hop the canonical link avoids.
        if is_alias_row(row):
            if sources:
                failures.append(f"{pid}: alias review route {review} is internally linked by "
                                f"{sources[:4]} — alias routes are redirects and must not be link targets")
            continue
        if len(sources) < INBOUND_LINK_FLOOR:
            failures.append(f"{pid}: review page {review} has {len(sources)} inbound internal "
                            f"link(s), floor is {INBOUND_LINK_FLOOR} (orphan class G5)")
    return check("link_graph_floor", not failures, pages_scanned=len(pages),
                 programs=inbound, failures=failures)


# ---------------------------------------------------------------------------
# 1d / 2b / 4b / 5b — surface-integrity checks (added 2026-08-06 after the
# operator read the shipped site and found four classes of drift the sensor
# above could not see: a hand-typed tally, a stale freshness stamp, one merchant
# listed twice, and a review page that never bound its own score.)
# ---------------------------------------------------------------------------
#: Routes the machine assembles from provider data. Blog articles are excluded
#: on purpose: their numbers and their bylines are editorial, not derived.
MACHINE_SURFACE_PREFIXES = ("best/", "cost/", "compare/", "reviews/",
                            "tools/", "quiz/", "how-we-rate/")
#: Routes whose "updated" stamp must track provider evidence rather than an editor.
PROVIDER_SURFACE_PREFIXES = ("best/", "cost/", "compare/", "reviews/")

_TALLY_CLASSES = {
    "provider": ("providers", "programs", "clinics"),
    "comparison": ("comparisons", "comparison pages", "comparison guides"),
    "guide": ("research guides", "evidence guides", "guides"),
}
_TALLY_NOUNS = sorted((noun for nouns in _TALLY_CLASSES.values() for noun in nouns),
                      key=len, reverse=True)
#: Adjectives a tally sentence may carry between the number and the noun. A
#: closed vocabulary on purpose: an open one turned "$99 starting price. Both
#: programs..." into a claim that the site covers 99 programs.
_TALLY_MODIFIER = (r"(?:online|curated|additional|more|other|source-bound|in-depth|"
                   r"independent|ranked|live|research|evidence|comparison|GLP[-‑]?1)\s+")
TALLY_COPY = re.compile(
    r"(?<![-‑\d])(\d{1,4}|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\s+"
    r"((?:" + _TALLY_MODIFIER + r"){0,3})(" + "|".join(_TALLY_NOUNS) + r")\b", re.I)
#: <dt>label</dt><dd>value</dd> — a metric tile states its tally as a bare number
#: whose meaning lives entirely in the label beside it. That pairing is exactly
#: what read as nonsense on the homepage ("Providers compared 29 / Comparisons 13").
_METRIC_PAIR = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S | re.I)
_GUIDE_LIBRARY_CARD = re.compile(
    r'<article\s+class="[^"]*\bpanel\b[^"]*\bevidence-line\b[^"]*"[^>]*>'
    r'.*?<h3>\s*<a\s+href="(/[a-z0-9][a-z0-9/-]*/)"',
    re.S | re.I,
)
_BLOCK_END = re.compile(
    r"</(?:p|div|section|li|dd|dt|dl|td|th|tr|h[1-6]|small|figcaption|aside|nav|"
    r"article|header|footer|main|span|a|strong|em|b)\s*>", re.I)


def _block_text(html: str) -> str:
    """Visible text with block boundaries preserved as sentence breaks.

    Flattening a page to one space-joined string invents adjacencies that no
    reader ever sees: a <dd> value runs straight into the next <dt> label."""
    return " ".join(TAGS.sub(" ", _BLOCK_END.sub(" ¶ ", html)).split())
_ISO_DATE_IN_HTML = re.compile(r"(?<![\d>])(20\d{2}-[01]\d-[0-3]\d)(?![\d])")
_DATE_MODIFIED = re.compile(r'"dateModified":"(\d{4}-\d{2}-\d{2})')
_DATE_PUBLISHED = re.compile(r'"datePublished":"(\d{4}-\d{2}-\d{2})"')
#: The two review templates name the block differently ("score" on the provider
#: templates, "score-status" on the clean-room READY6 pages). Match both, or the
#: sensor reads a scored page as having no score block at all.
_SCORE_BLOCK = re.compile(r'<p class="score(?:-status)?(?:[^"]*)"[^>]*>(.*?)</p>', re.S)
_RENDERED_SCORE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*/\s*10")
PENDING_SCORE = re.compile(
    r"score pending|pending score|rating pending|score unavailable|"
    r"\bTBD\b|coming soon|to be determined|placeholder", re.I)
#: A pending-score state is honest only when the page says WHY, in the same block.
PENDING_REASON = re.compile(
    r"\b(because|no numeric|does not support|not enough|insufficient|"
    r"no comparable|source packet|evidence gap|remain(?:s)? pending)\b", re.I)


def _routes(release: Path, prefixes: tuple[str, ...]) -> dict[str, str]:
    public = release / "public"
    out: dict[str, str] = {}
    if not public.is_dir():
        return out
    for path in sorted(public.rglob("index.html")):
        rel = path.relative_to(public).as_posix()
        if rel.startswith(prefixes) or rel == "index.html" or rel == "guides/index.html":
            try:
                out[rel] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return out


def _rendered_program_ids(html: str) -> set[str]:
    return set(re.findall(r'data-program="([a-z0-9-]+)"', html))


def _rendered_provider_labels(html: str) -> list[tuple[str, str]]:
    """(program_id, display label) for every provider a surface list renders."""
    pairs = re.findall(r'data-program="([a-z0-9-]+)" data-provider="([^"]*)"', html)
    pairs += re.findall(r'<option value="([a-z0-9-]+)"[^>]*>([^<]*)</option>', html)
    pairs += re.findall(r'"program_id":"([a-z0-9-]+)","provider":"([^"]*)"', html)
    return [(pid, label) for pid, label in pairs if label.strip()]


def check_derived_tallies(release: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every tally a reader sees is reproducible from the shipped tree.

    THE BLIND SPOT THIS KILLS
    -------------------------
    check_count_copy only ever compared provider counts against the live count.
    The homepage also states a comparison count and a research-guide count, and
    a money page states its own row count. Any of those could be hand-typed and
    stay wrong forever, because nothing recomputed them (operator, 2026-08-06:
    "13 comparisons, 41 research guides").
    """
    pages = _routes(release, MACHINE_SURFACE_PREFIXES)
    if not pages:
        return check("derived_tallies", False, failures=["no built public/ tree to scan"])
    live = len(live_ids(rows))
    comparison_pages = sorted(r for r in pages
                              if r.startswith(("best/", "cost/", "compare/"))
                              and _rendered_program_ids(pages[r]))
    guide_index = pages.get("guides/index.html", "")
    # The research library may deliberately list a research article on a
    # commercial-intent route (for example /best/ozempic-alternatives/). Count
    # the unique article cards the library actually ships, not only /guides/
    # URL prefixes. Featured cards repeat the first entries, hence the set.
    guide_routes = set(_GUIDE_LIBRARY_CARD.findall(guide_index))
    if not guide_routes:
        # 2026-08-27 redesign: /guides/ retired into /insights/ (paginated),
        # which sits outside MACHINE_SURFACE_PREFIXES — read it from disk. Card
        # markup differs from the legacy library, so accept any /guides/ article
        # href alongside the legacy card pattern.
        insights_dir = release / "public" / "insights"
        if insights_dir.is_dir():
            for page_file in sorted(insights_dir.rglob("index.html")):
                body = page_file.read_text(errors="replace")
                guide_routes |= set(_GUIDE_LIBRARY_CARD.findall(body))
                guide_routes |= set(re.findall(r'href="(/guides/[a-z0-9-]+/)"', body))
    derived = {"live_providers": live, "comparison_pages": len(comparison_pages),
               "research_guides": len(guide_routes)}
    failures: list[str] = []
    seen: dict[str, Any] = {}
    def classify(noun: str) -> str | None:
        noun = noun.strip().lower()
        for klass, nouns in _TALLY_CLASSES.items():
            if noun in nouns or noun.rstrip("s") + "s" in nouns:
                return klass
        return None

    for route, html in sorted(pages.items()):
        rows_here = len(_rendered_program_ids(html))
        options = len(re.findall(r'<option value="[a-z0-9-]+"', html))
        own_guides = len(set(re.findall(r'href="(/guides/[a-z0-9-]+/)"', html)))
        allowed = {
            "provider": {live} | ({rows_here} if rows_here else set()) | ({options} if options else set()),
            "comparison": {len(comparison_pages)},
            "guide": {len(guide_routes)} | ({own_guides} if own_guides else set()),
        }
        found = []

        def record(phrase: str, klass: str, value: int) -> None:
            ok = value in allowed[klass]
            found.append({"phrase": phrase, "class": klass, "value": value, "ok": ok})
            if not ok:
                failures.append(
                    f"/{route}: states {phrase!r} but the only {klass} tallies derivable "
                    f"from this release are {sorted(allowed[klass])} — a hand-typed total "
                    "cannot self-correct")

        for match in TALLY_COPY.finditer(_block_text(html)):
            raw = match.group(1).lower()
            value = NUMBER_WORDS.get(raw) if not raw.isdigit() else int(raw)
            klass = classify(match.group(3))
            if value is None or klass is None:
                continue
            record(" ".join(match.group(0).split()), klass, value)
        # Metric tiles: the number is bare, so the label is the whole claim.
        for pair in _METRIC_PAIR.finditer(html):
            label = " ".join(TAGS.sub(" ", pair.group(1)).split())
            value_text = " ".join(TAGS.sub(" ", pair.group(2)).split())
            if not value_text.isdigit():
                continue
            words = [w.strip(".,·").lower() for w in label.split()]
            klass = next((c for w in words if (c := classify(w))), None)
            if klass is None:
                continue
            record(f"{label} = {value_text}", klass, int(value_text))
        if found:
            seen[f"/{route}"] = found
    return check("derived_tallies", not failures, derived=derived,
                 routes_scanned=len(pages), tallies=seen, failures=failures)


def check_freshness_stamps(release: Path) -> dict[str, Any]:
    """A provider surface may not claim to be older than the evidence it shows.

    THE BLIND SPOT THIS KILLS
    -------------------------
    A provider wave merged 2026-08-06 research into every comparison row while
    every money page kept the hand stamp "Updated 2026-07-24". Nothing compared
    the stamp to the page's own dated evidence, so the site advertised week-old
    numbers it had refreshed that morning.
    """
    pages = _routes(release, PROVIDER_SURFACE_PREFIXES)
    pages = {r: h for r, h in pages.items() if r.startswith(PROVIDER_SURFACE_PREFIXES)}
    if not pages:
        return check("freshness_stamps", False, failures=["no provider surfaces in built tree"])
    failures: list[str] = []
    per_route: dict[str, Any] = {}
    for route, html in sorted(pages.items()):
        stamp_match = _DATE_MODIFIED.search(html)
        if not stamp_match:
            failures.append(f"/{route}: provider surface ships no dateModified stamp")
            per_route[f"/{route}"] = {"stamp": None}
            continue
        stamp = stamp_match.group(1)
        body = _DATE_PUBLISHED.sub("", _DATE_MODIFIED.sub("", html))
        body = body.replace(f"Updated {stamp}", "")
        evidence = sorted(set(_ISO_DATE_IN_HTML.findall(body)))
        newest = evidence[-1] if evidence else None
        per_route[f"/{route}"] = {"stamp": stamp, "newest_evidence": newest}
        if newest and stamp < newest:
            failures.append(
                f"/{route}: stamped 'updated {stamp}' while rendering evidence dated "
                f"{newest} — the freshness stamp is a hand date, not derived from the "
                "provider data the page shows")
    return check("freshness_stamps", not failures, routes_scanned=len(pages),
                 per_route=per_route, failures=failures)


def check_aeo_surface(release: Path, apply_baseline: bool = True) -> dict[str, Any]:
    """Check #17 — the answer-engine surface, made un-deployable when it regresses.

    THE BLIND SPOT THIS KILLS
    -------------------------
    Every AEO defect the 2026-08-19 baseline measured was invisible to this
    sensor: 68 pages shipping two `Article` nodes, 68 pages whose citation links
    read the literal words "Open source", an empty `<time datetime="">`, 55 of 100
    sitemap URLs with no `lastmod`, no `llms.txt` at all, and a `Dataset` node
    with no `distribution`. None of it broke a build, so all of it shipped.

    Answer engines are where the money is nearest for us (positions 5-9 on Bing,
    which is ChatGPT's retrieval surface), and the moat is that our dates and
    citations mean something. So these become deploy-blocking, exactly like the
    monetization regressions check #16 already blocks.

    Seven sub-checks, all zero-tolerance:
      1. `llms.txt` present, parses, every listed URL in-tree + indexable
      2. sitemap `lastmod` on 100% of URLs
      3. zero pages carrying more than one Article-family JSON-LD node
      4. zero literal "Open source" anchors (server-rendered or in site.js)
      5. zero empty `<time datetime="">` elements
      6. visible dateline == JSON-LD `dateModified` on every page carrying both
      7. Price Index `Dataset` carries distribution/variableMeasured/license and
         the distribution files exist with row counts matching the rendered table
    """
    public = release / "public"
    if not public.is_dir():
        return check("aeo_surface", False, failures=[f"public tree missing: {public}"])

    failures: list[str] = []
    detail: dict[str, Any] = {}

    # --- 1. llms.txt -------------------------------------------------------
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import glp_llms_txt_gate  # type: ignore
        llms = glp_llms_txt_gate.evaluate(release)
        detail["llms_txt"] = {"status": llms["status"], "listed_url_count": llms["listed_url_count"]}
        failures.extend(llms["failures"])
    except Exception as exc:  # pragma: no cover - import/enviro failure is a real failure
        failures.append(f"llms_txt_gate_error:{type(exc).__name__}: {exc}")

    # --- 2. sitemap lastmod coverage ---------------------------------------
    sitemap_path = public / "sitemap.xml"
    if not sitemap_path.is_file():
        failures.append("sitemap_missing")
    else:
        entries = _SITEMAP_URL_BLOCK.findall(sitemap_path.read_text(encoding="utf-8"))
        total = len(entries)
        missing = [block for block in entries if "<lastmod>" not in block]
        detail["sitemap"] = {"url_count": total, "without_lastmod": len(missing)}
        if total == 0:
            failures.append("sitemap_has_no_urls")
        for block in missing:
            loc = re.search(r"<loc>(.*?)</loc>", block, re.DOTALL)
            failures.append(f"sitemap_lastmod_missing:{loc.group(1).strip() if loc else '?'}")

    # --- 3-6. per-page HTML/JSON-LD defects --------------------------------
    dup_article: list[str] = []
    open_source: list[str] = []
    empty_time: list[str] = []
    dateline_mismatch: list[str] = []
    pages = 0
    for page in sorted(public.rglob("index.html")):
        if page.is_symlink():
            continue
        pages += 1
        html = page.read_text(encoding="utf-8", errors="replace")
        rel = page.parent.relative_to(public).as_posix()
        route = "/" if rel == "." else f"/{rel}/"

        article_nodes = 0
        json_dates: set[str] = set()
        for block in _LD_JSON_BLOCK.findall(html):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                failures.append(f"jsonld_unparseable:{route}")
                continue
            for node in _flatten_ld(payload):
                types = node.get("@type")
                types = types if isinstance(types, list) else ([types] if types else [])
                if any(str(t) in _ARTICLE_LD_TYPES for t in types):
                    article_nodes += 1
                value = node.get("dateModified")
                if isinstance(value, str) and value.strip():
                    json_dates.add(value.strip()[:10])
        if article_nodes > 1:
            dup_article.append(route)

        if _OPEN_SOURCE_ANCHOR.search(html):
            open_source.append(route)
        if _EMPTY_TIME.search(html):
            empty_time.append(route)

        visible = {m for m in _TIME_DATETIME.findall(html) if _ISO_DATE_ONLY.match(m)}
        if json_dates and visible and not (json_dates & visible):
            dateline_mismatch.append(
                f"{route}: dateModified={sorted(json_dates)} visible={sorted(visible)}")

    detail["pages_scanned"] = pages
    for route in dup_article:
        failures.append(f"duplicate_article_nodes:{route}")
    for route in open_source:
        failures.append(f"open_source_anchor:{route}")
    for route in empty_time:
        failures.append(f"empty_time_element:{route}")
    for entry in dateline_mismatch:
        failures.append(f"dateline_ne_datemodified:{entry}")

    site_js = public / "assets/site.js"
    if site_js.is_file() and _OPEN_SOURCE_ANCHOR.search(site_js.read_text(encoding="utf-8",
                                                                          errors="replace")):
        failures.append("open_source_anchor:assets/site.js runtime citation label")

    # --- 7. Price Index Dataset -------------------------------------------
    price_index = public / "glp-1-price-index/index.html"
    if price_index.is_file():
        html = price_index.read_text(encoding="utf-8", errors="replace")
        dataset: dict[str, Any] | None = None
        for block in _LD_JSON_BLOCK.findall(html):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            for node in _flatten_ld(payload):
                types = node.get("@type")
                types = types if isinstance(types, list) else ([types] if types else [])
                if "Dataset" in [str(t) for t in types]:
                    dataset = node
        if dataset is None:
            failures.append("price_index_dataset_node_missing")
        else:
            for field in ("distribution", "variableMeasured", "license"):
                if not dataset.get(field):
                    failures.append(f"price_index_dataset_missing_field:{field}")
            # Rendered observation rows = <tr> inside <tbody> (header rows live in
            # <thead>); fall back to all <tr> minus one header row per table.
            bodies = re.findall(r"<tbody\b.*?</tbody>", html, re.DOTALL)
            if bodies:
                rendered_rows = sum(len(re.findall(r"<tr\b", body)) for body in bodies)
            else:
                rendered_rows = max(0, len(re.findall(r"<tr\b", html))
                                    - len(re.findall(r"<table\b", html)))
            distributions = dataset.get("distribution") or []
            if isinstance(distributions, dict):
                distributions = [distributions]
            detail["price_index"] = {"distribution_count": len(distributions),
                                     "rendered_row_estimate": rendered_rows}
            for entry in distributions:
                url = str((entry or {}).get("contentUrl") or "")
                rel = url.split("glpclinicscout.com/", 1)[-1].lstrip("/")
                target = public / rel
                if not rel or not target.is_file():
                    failures.append(f"price_index_distribution_file_missing:{url or '<no contentUrl>'}")
                    continue
                if target.suffix == ".csv":
                    rows = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
                    data_rows = max(0, len(rows) - 1)
                elif target.suffix == ".json":
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    records = payload if isinstance(payload, list) else (
                        payload.get("observations") or payload.get("rows") or [])
                    data_rows = len(records)
                else:
                    continue
                detail.setdefault("distribution_rows", {})[rel] = data_rows
                if data_rows == 0:
                    failures.append(f"price_index_distribution_empty:{rel}")

    # --- ratchet -----------------------------------------------------------
    # Check #17 lands on a tree that already carries 259 measured AEO defects
    # (B-08's remediation batch is what clears them). Two bad options were on the
    # table: soften the check, or block every deploy — including the article
    # carrier — until the whole batch ships. Neither is acceptable.
    #
    # So the check is a RATCHET. A release may carry
    # `data/aeo-surface-baseline.json` enumerating the exact pre-existing defect
    # strings, minted deliberately with a receipt. Those are reported as accepted
    # known defects. ANY defect not in that list fails the deploy immediately, and
    # the baseline can only ever be shrunk by remediation — a defect class that
    # grows fails even if its class is listed. Absent the file, every failure is
    # hard, so strict is the default and the escape is explicit and dated.
    if not apply_baseline:
        return check("aeo_surface", not failures, failures=failures, **detail)
    accepted, unaccepted, resolved, baseline_meta = _apply_aeo_baseline(release, failures)
    if baseline_meta is not None:
        detail["aeo_baseline"] = baseline_meta
        detail["accepted_known_defects"] = len(accepted)
        detail["resolved_since_baseline"] = len(resolved)
        detail["total_defects_observed"] = len(failures)
    return check("aeo_surface", not unaccepted, failures=unaccepted, **detail)


def _apply_aeo_baseline(release: Path, failures: list[str]):
    """Split observed failures into accepted-known vs must-fail, per the ratchet."""
    path = release / "data/aeo-surface-baseline.json"
    if not path.is_file():
        return [], failures, [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [*failures, f"aeo_baseline_unparseable:{exc}"], [], None
    if payload.get("schema") != AEO_BASELINE_SCHEMA:
        return [], [*failures, f"aeo_baseline_schema_invalid:{payload.get('schema')}"], [], None
    accepted_set = set(payload.get("accepted_failures") or [])
    if not payload.get("minted_at_utc") or not payload.get("remediation_item"):
        return [], [*failures, "aeo_baseline_missing_provenance"], [], None

    accepted = [f for f in failures if f in accepted_set]
    unaccepted = [f for f in failures if f not in accepted_set]
    resolved = sorted(accepted_set - set(failures))
    meta = {
        "path": str(path),
        "minted_at_utc": payload["minted_at_utc"],
        "remediation_item": payload["remediation_item"],
        "accepted_failure_count": len(accepted_set),
        "note": payload.get("note"),
    }
    return accepted, unaccepted, resolved, meta


AEO_BASELINE_SCHEMA = "glp-aeo-surface-baseline-v1"


def mint_aeo_baseline(release: Path, remediation_item: str, note: str = "") -> dict[str, Any]:
    """Freeze the CURRENT defect set as accepted-known. Ratchet only: never widens
    silently, because the caller has to name the backlog item that will clear it."""
    result = check_aeo_surface(release, apply_baseline=False)
    observed = sorted(result.get("failures") or [])
    return {
        "schema": AEO_BASELINE_SCHEMA,
        "minted_at_utc": utc(),
        "release": release.name,
        "remediation_item": remediation_item,
        "note": note,
        "accepted_failures": observed,
        "accepted_failure_count": len(observed),
    }


_SITEMAP_URL_BLOCK = re.compile(r"<url>.*?</url>", re.DOTALL)
_LD_JSON_BLOCK = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE)
_OPEN_SOURCE_ANCHOR = re.compile(r"Open source(?:\s*[↗>»])?", re.IGNORECASE)
_EMPTY_TIME = re.compile(r"""<time[^>]*\sdatetime=["']\s*["']""", re.IGNORECASE)
_TIME_DATETIME = re.compile(r"""<time[^>]*\sdatetime=["']([^"']*)["']""", re.IGNORECASE)
_ISO_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ARTICLE_LD_TYPES = {"Article", "NewsArticle", "BlogPosting", "TechArticle",
                     "Report", "ScholarlyArticle", "MedicalScholarlyArticle"}


def _flatten_ld(value: Any, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = [] if out is None else out
    if isinstance(value, list):
        for item in value:
            _flatten_ld(item, out)
        return out
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _flatten_ld(item, out)
        if any(key != "@context" and key != "@graph" for key in value):
            out.append(value)
        for key, item in value.items():
            if key == "@graph":
                continue  # already flattened above; re-descending double-counts nodes
            if isinstance(item, (dict, list)):
                _flatten_ld(item, out)
    return out


def check_identity_dedup(release: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One merchant, one render identity, everywhere a list is assembled.

    THE BLIND SPOT THIS KILLS
    -------------------------
    Awin program `shedrx` ("Shed") and Katalys offer `katalys-1516` ("SHED") are
    the same merchant (both source to shedrx.com). Two wire-in waves minted two
    render identities, so the calculator offered Shed twice, the compare table
    listed it twice, and the live provider count was one too high.
    """
    failures: list[str] = []
    # 1. Registry: a second live row for one brand must declare the relationship.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not _live_selector(row):
            continue
        groups.setdefault(identity_key(row.get("display_name")), []).append(row)
    declared: dict[str, Any] = {}
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        canonical = [m for m in members if not is_alias_row(m)]
        aliases = [m for m in members if is_alias_row(m)]
        declared[key] = {"canonical": [str(m.get("program_id")) for m in canonical],
                         "aliases": [str(m.get("program_id")) for m in aliases]}
        if len(canonical) != 1:
            failures.append(
                f"registry: {len(members)} live rows share brand identity {key!r} "
                f"({[str(m.get('program_id')) for m in members]}) and "
                f"{len(canonical)} of them claim to be canonical — exactly one must")
            continue
        for alias in aliases:
            decl = _alias_declaration(alias)
            reason = str(decl.get("reason") or "").strip()
            if decl.get("canonical_program_id") != str(canonical[0].get("program_id")):
                failures.append(f"registry: alias {alias.get('program_id')} names "
                                f"{decl.get('canonical_program_id')!r} as canonical, but the "
                                f"canonical row for {key!r} is {canonical[0].get('program_id')}")
            if len(reason) <= 40:
                failures.append(f"registry: alias {alias.get('program_id')} carries no written "
                                "reason for being a second row of one merchant")
    # 2. Shipped surface lists: no brand may appear twice, whatever the casing.
    pages = _routes(release, MACHINE_SURFACE_PREFIXES)
    per_surface: dict[str, Any] = {}
    for route, html in sorted(pages.items()):
        labels = _rendered_provider_labels(html)
        if not labels:
            continue
        by_key: dict[str, list[str]] = {}
        for pid, label in labels:
            by_key.setdefault(identity_key(label), []).append(f"{label} ({pid})")
        dupes = {k: v for k, v in by_key.items() if len(set(v)) > 1}
        per_surface[f"/{route}"] = {"entries": len(labels), "distinct_brands": len(by_key)}
        for key, members in sorted(dupes.items()):
            failures.append(
                f"/{route}: brand {key!r} is listed {len(set(members))} times as "
                f"{sorted(set(members))} — one merchant, one row")
    return check("identity_dedup", not failures, declared_alias_groups=declared,
                 surfaces=per_surface, failures=failures)


_OPTION_TAG = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.S | re.I)
_SELECT_TAG = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.S | re.I)
_ATTR_VALUE = re.compile(r'value="([^"]*)"', re.I)
_ATTR_NAMED = re.compile(r'(?:id|name)="([^"]*)"', re.I)
_PAYLOAD_TAG = re.compile(r"window\.(GCS_[A-Z0-9_]+)\s*=\s*(\[.*?\]|\{.*?\})\s*;", re.S)


def _selectable_medication_lists(node: Any, trail: str, out: list[tuple[str, list[dict]]]) -> None:
    if isinstance(node, list):
        rows = [item for item in node if isinstance(item, dict)]
        if len(rows) > 1 and all("medication_id" in row for row in rows):
            out.append((trail, rows))
        for index, item in enumerate(node):
            _selectable_medication_lists(item, f"{trail}[{index}]", out)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            label = f"{node['program_id']}.{key}" if node.get("program_id") and key == "medications" else key
            _selectable_medication_lists(value, f"{trail}.{label}" if trail else label, out)


def check_selectable_option_dedup(release: Path) -> dict[str, Any]:
    """No shipped surface offers two options a reader cannot tell apart.

    THE BLIND SPOT THIS KILLS
    -------------------------
    2026-08-18, operator: the cost calculator listed "Compounded tirzepatide
    (injection)" TWICE for enhance-md. Both are real - enhance.md publishes a
    "GLP-1 Advanced" tier at $180-then-$280 and a "Microdosing Tirzepatide" tier
    at $169 flat - and both map to the same closed-list medication_id. The page
    rendered the medication_id's label for both AND selected the row by
    medication_id, so picking the second option showed the FIRST one's price,
    quote and capture date. The $169 row was unreachable.

    `check_identity_dedup` asks the same question one level up ("one merchant,
    one row"). This asks it of the options INSIDE a provider: an option a reader
    cannot distinguish is an option that can hand them the wrong number.
    """
    failures: list[str] = []
    per_route: dict[str, Any] = {}
    public = release / "public"
    if not public.is_dir():
        return check("selectable_option_dedup", False, failures=["no built public/ tree to scan"])
    pages = selects = payload_lists = 0
    for path in sorted(public.rglob("*.html")):
        route = "/" + path.relative_to(public).as_posix()
        html_text = path.read_text(encoding="utf-8", errors="replace")
        pages += 1
        found: list[str] = []
        for match in _SELECT_TAG.finditer(html_text):
            selects += 1
            named = _ATTR_NAMED.search(match.group(1))
            name = named.group(1) if named else "select"
            options = []
            for option in _OPTION_TAG.finditer(match.group(2)):
                value_match = _ATTR_VALUE.search(option.group(1))
                text = " ".join(TAGS.sub(" ", option.group(2)).split())
                if not text or not (value_match and value_match.group(1)):
                    continue
                options.append((value_match.group(1), text))
            for position, field in ((1, "text"), (0, "value")):
                seen: dict[str, int] = {}
                for option in options:
                    seen[option[position]] = seen.get(option[position], 0) + 1
                for key, count in seen.items():
                    if count > 1:
                        found.append(f"{route}#{name}: {count} options share the same {field} "
                                     f"{key!r} — a reader cannot tell them apart")
        for payload in _PAYLOAD_TAG.finditer(html_text):
            try:
                parsed = json.loads(payload.group(2))
            except ValueError:
                continue
            lists: list[tuple[str, list[dict]]] = []
            _selectable_medication_lists(parsed, payload.group(1), lists)
            for trail, entries in lists:
                payload_lists += 1
                labels = [row.get("display_label") or row.get("label") for row in entries]
                keys = [row.get("entry_key") or row.get("medication_id") for row in entries]
                for field, values in (("label", labels), ("entry_key", keys)):
                    for value in sorted({v for v in values if values.count(v) > 1 and v}):
                        found.append(f"{route}:{trail}: two selectable medication entries share "
                                     f"the same {field} {value!r} — the page cannot show the "
                                     "right price for both")
        if found:
            failures.extend(found)
            per_route[route] = found
    return check("selectable_option_dedup", not failures, pages_scanned=pages,
                 selects_scanned=selects, embedded_medication_lists=payload_lists,
                 per_route=per_route, failures=failures)


def check_score_binding(release: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A live provider's review page renders its real score, or says why not.

    THE BLIND SPOT THIS KILLS
    -------------------------
    The fast-path review template hard-coded the string "Score pending evidence"
    into its summary block. The same provider carried a derived merit score on
    the comparison table the same build (Measured: 7.6/10). The score existed;
    the page just never bound it, and nothing compared the two surfaces.
    """
    public = release / "public"
    if not public.is_dir():
        return check("score_binding", False, failures=["no built public/ tree to scan"])
    # Scores as rendered on the comparison surfaces, keyed by program id.
    elsewhere: dict[str, str] = {}
    for path in sorted(public.rglob("index.html")):
        rel = path.relative_to(public).as_posix()
        if not rel.startswith(("best/", "cost/", "compare/")):
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'data-program="([a-z0-9-]+)" data-provider="[^"]*">(.{0,4000}?)</tr>',
                                 html, re.S):
            # The table splits "7.6" and "/10" across elements, so read the row
            # the way a reader does rather than as raw bytes.
            found = _RENDERED_SCORE.search(_visible_text(match.group(2)))
            if found:
                elsewhere.setdefault(match.group(1), found.group(1))
    failures: list[str] = []
    per_program: dict[str, Any] = {}
    by_render = {render_id(row): row for row in rows}
    for pid in live_ids(rows):
        row = by_render[pid]
        review = str(row.get("review_path") or "").strip("/")
        page = public / review / "index.html"
        if not page.is_file():
            failures.append(f"{pid}: live provider has no shipped review page at /{review}/")
            continue
        html = page.read_text(encoding="utf-8", errors="replace")
        blocks = _SCORE_BLOCK.findall(html)
        block_text = _visible_text(" ".join(blocks)) if blocks else ""
        rendered = _RENDERED_SCORE.search(block_text)
        pending = bool(PENDING_SCORE.search(block_text))
        table_score = elsewhere.get(pid) or elsewhere.get(str(row.get("program_id")))
        per_program[pid] = {"review": f"/{review}/", "score_block": block_text[:80] or None,
                            "rendered_score": rendered.group(1) if rendered else None,
                            "score_on_comparison_surface": table_score}
        if rendered:
            if table_score and rendered.group(1) != table_score:
                failures.append(f"{pid}: review page shows {rendered.group(1)}/10 while the "
                                f"comparison table shows {table_score}/10 for the same build")
            continue
        if table_score:
            failures.append(
                f"{pid}: review page /{review}/ ships {block_text.strip()!r} while this same "
                f"release scores it {table_score}/10 on the comparison table — the merit score "
                "was never bound to the review render")
        elif pending and not PENDING_REASON.search(_visible_text(html)):
            failures.append(
                f"{pid}: review page /{review}/ ships the placeholder {block_text.strip()!r} "
                "with no written reason a reader can act on")
        elif not block_text:
            failures.append(f"{pid}: review page /{review}/ renders no score block at all")
    return check("score_binding", not failures, programs=per_program,
                 comparison_surface_scores=len(elsewhere), failures=failures)


def provider_runtime_configuration(release: Path, *, live: bool) -> dict[str, Any]:
    """One effective production configuration, never a gate minted per entry."""
    import tomllib
    config_path = release / "wrangler.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    if config.get("name", CF_PROJECT if live else None) != CF_PROJECT:
        raise ValueError("runtime_project_mismatch")
    values = dict(config.get("vars") or {})
    bindings = config.get("d1_databases") or []
    if live:
        import requests
        token = load_env_value("CLOUDFLARE_API_TOKEN") or load_env_value("CF_API_TOKEN")
        if not token:
            raise ValueError("runtime_configuration_credentials_unavailable")
        body = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/pages/projects/{CF_PROJECT}",
            headers={"Authorization": "Bearer " + token}, timeout=30).json()
        if not body.get("success") or body.get("result", {}).get("name") != CF_PROJECT:
            raise ValueError("runtime_configuration_readback_failed")
        production = body["result"]["deployment_configs"]["production"]
        values = {**{k: v.get("value") for k, v in production.get("env_vars", {}).items()}, **values}
        # Wrangler's explicit binding wins when present; otherwise Pages retains
        # its configured production binding.
        if not bindings:
            bindings = [{"binding": k, "database_id": v.get("id")} for k, v in production.get("d1_databases", {}).items()]
    expected_d1 = "bcb91d2d-c3f1-4c55-93e3-070587e86d60"
    if values.get("AFFILIATE_RUNTIME_MODE") != "production":
        raise ValueError("runtime_mode_unestablished")
    gate = values.get("AFFILIATE_LIVE_GATE")
    if not isinstance(gate, str) or not re.fullmatch(r"operator-approved:[a-f0-9]{64}", gate):
        raise ValueError("runtime_gate_unestablished")
    if [b.get("database_id") for b in bindings if b.get("binding") == "GLP_DB"] != [expected_d1]:
        raise ValueError("runtime_d1_binding_unestablished")
    return {"AFFILIATE_RUNTIME_MODE": "production", "AFFILIATE_LIVE_GATE": gate,
            "GLP_DB_ID": expected_d1, "project": CF_PROJECT,
            "source": "cloudflare_production_plus_candidate" if live else "candidate_wrangler"}


def check_publishable_provider_routes(release: Path, *, live_configuration: bool = False,
                                     runtime_root: Path | None = None) -> dict[str, Any]:
    """All rendered provider identities, including held/unregistered reviews.

    Execute the shipped handler offline with an in-memory mapping sink. Never
    visit affiliate URLs or claim this predeploy contract is production D1 proof.
    """
    from html.parser import HTMLParser
    from urllib.parse import urlsplit

    identities: set[str] = set()

    class Providers(HTMLParser):
        def __init__(self):
            super().__init__()
            self.provider_select = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            for key in ("data-program", "data-provider-record"):
                if attrs.get(key):
                    identities.add(attrs[key])
            if tag == "select":
                self.provider_select = any("provider" in str(v).lower() for k, v in attrs.items() if k in ("id", "name", "data-role", "aria-label"))
            if tag == "option" and self.provider_select and attrs.get("value"):
                identities.add(attrs["value"])
            if tag == "a":
                parsed = urlsplit(attrs.get("href", ""))
                if parsed.netloc and parsed.netloc != "glpclinicscout.com":
                    return
                match = re.fullmatch(r"/(?:reviews|providers|go)/([^/]+)/?", parsed.path)
                if match and match.group(1) != "submit":
                    identities.add(match.group(1))

        def handle_endtag(self, tag):
            if tag == "select":
                self.provider_select = False

    try:
        rows = registry_rows(release)
        public = release / "public"
        if not public.is_dir():
            raise ValueError("public_missing")
        parser = Providers()
        for page in public.rglob("*.html"):
            body, deferred_failures = _include_deferred_provider_payloads(release, page.read_text(encoding="utf-8", errors="replace"))
            if deferred_failures:
                raise ValueError("deferred_provider_payload_unestablished")
            parser.feed(body)
            identities.update(re.findall(r'"program_id"\s*:\s*"([a-z0-9-]+)"', body))
            relative = page.relative_to(public).parts
            # /reviews/submit is the registered review intake, not a provider
            # (src/pass2-review-hub.mjs renderReviewSubmit).
            if len(relative) == 3 and relative[0] in ("reviews", "providers") and relative[1] != "submit" and relative[2] == "index.html":
                identities.add(relative[1])
        for row in rows:
            profile = _profile(row)
            if profile.get("wired") is True and any((profile.get("surfaces") or {}).values()):
                identities.add(render_id(row))
        providers = {}
        for identity in sorted(identities):
            matches = [r for r in rows if identity in (str(r.get("program_id")), render_id(r))]
            row = matches[0] if len(matches) == 1 else {}
            slug = render_id(row) if row else identity
            providers[slug] = {"slug": slug, "program_id": row.get("program_id"),
                               "registered": bool(row), "commercial_state": row.get("commercial_state")}
        if not providers:
            raise ValueError("publishable_provider_set_unestablished")
        configuration_error = None
        try:
            configuration = provider_runtime_configuration((runtime_root or release).resolve(), live=live_configuration)
        except Exception as exc:
            configuration = {}
            configuration_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        source_error = None
        actual_root = (runtime_root or release).resolve()
        try:
            # Pages selects specific files/nested index routes before the
            # dynamic handler. Equal source closures alone do not prove which
            # handler executes; this sensor supports only the registered shape.
            competing = sorted(
                p.relative_to(actual_root / "functions").as_posix()
                for p in (actual_root / "functions/go").rglob("*")
                if p.is_file() and p.suffix in {".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
                and p.relative_to(actual_root / "functions/go").as_posix()
                not in {"[program].js", "program-manifest.js"})
            if competing:
                raise ValueError("runtime_route_shadow_unestablished:" + ",".join(competing))
            if (public / "_worker.js").exists() or any(
                    list((actual_root / directory).glob("_middleware.*"))
                    for directory in ("functions", "functions/go")):
                raise ValueError("runtime_override_unestablished")
            def closure(root):
                result = {}
                for directory in ("functions", "src", "data"):
                    if not (root / directory).is_dir():
                        raise ValueError("runtime_source_missing")
                    for item in (root / directory).rglob("*"):
                        if item.is_file():
                            result[str(item.relative_to(root))] = hashlib.sha256(item.read_bytes()).hexdigest()
                return result
            if closure(actual_root) != closure(release):
                raise ValueError("runtime_source_mismatch")
        except Exception as exc:
            source_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        proc = subprocess.run(
            ["node", str(BUSINESS / "scripts/glp_deploy_provider_routes.mjs"), str(release.resolve())],
            input=json.dumps({"providers": list(providers.values()), "configuration": configuration,
                              "configuration_error": configuration_error, "source_error": source_error}),
            text=True, capture_output=True, timeout=120)
        if proc.returncode:
            raise ValueError("candidate_route_runtime_unestablished")
        verdicts = json.loads(proc.stdout)
        if sorted(v["slug"] for v in verdicts) != sorted(providers):
            raise ValueError("route_census_incomplete")
        routes_file = public / "_routes.json"
        if routes_file.exists():
            import fnmatch
            routes = load_json(routes_file)
            if routes.get("version") != 1 or not isinstance(routes.get("include"), list) or not isinstance(routes.get("exclude"), list):
                raise ValueError("runtime_routes_unestablished")
            for verdict in verdicts:
                route = verdict.get("route", "")
                if not any(fnmatch.fnmatchcase(route, p) for p in routes["include"]) or any(fnmatch.fnmatchcase(route, p) for p in routes["exclude"]):
                    verdict["failures"].append("runtime_route_excluded")
        failures = [f"{v['slug']}: {reason}" for v in verdicts for reason in v["failures"]]
        return check("publishable_provider_routes", not failures, providers=verdicts,
                     provider_count=len(providers), failures=failures,
                     runtime_configuration=configuration, runtime_source_root=str(actual_root),
                     proof="candidate handler with in-memory D1; no network or production conversion")
    except Exception as exc:
        names = sorted(identities) or ["unknown-provider-set"]
        return check("publishable_provider_routes", False, provider_count=None,
                     failures=[f"{name}: route_state_unestablished ({type(exc).__name__})" for name in names])


def check_base_chain(offline: bool) -> dict[str, Any]:
    if not CONTENT_BASE.is_file():
        return check("base_chain", False, failures=[f"{CONTENT_BASE} missing"])
    base = load_json(CONTENT_BASE)
    try:
        validate_pointer_shape(
            base,
            releases_root=RELEASES,
            require_release_dirs=True,
        )
    except RuntimeError as exc:
        return check(
            "base_chain",
            False,
            failures=[f"current-content-base.json schema invalid: {exc}"],
        )
    base_deployment = str(base.get("deployment_id") or "")
    if not base_deployment:
        return check("base_chain", False, base_deployment_id=None,
                     failures=["current-content-base.json carries no deployment_id"])
    if offline:
        return skipped("base_chain", "offline mode: Cloudflare not queried",
                       base_deployment_id=base_deployment)
    try:
        import requests  # noqa: PLC0415 — optional at import time for offline use
    except ImportError:
        return skipped("base_chain", "requests unavailable", base_deployment_id=base_deployment)
    token = load_env_value("CLOUDFLARE_API_TOKEN") or load_env_value("CF_API_TOKEN")
    if not token:
        return skipped("base_chain", "no Cloudflare token available",
                       base_deployment_id=base_deployment)
    api = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/pages/projects/{CF_PROJECT}"
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token,
                            "User-Agent": "glp-deploy-completeness-sensor/1.0"})
    live_deployment = None
    try:
        body = session.get(api, timeout=60).json()
        if body.get("success") and isinstance(body.get("result"), dict):
            canonical = body["result"].get("canonical_deployment")
            if isinstance(canonical, dict):
                live_deployment = canonical.get("id")
    except Exception as exc:  # network/API shape — report, never crash the deploy
        return check("base_chain", False, base_deployment_id=base_deployment,
                     failures=[f"cloudflare_query_failed: {type(exc).__name__}"])
    if not live_deployment:
        return check("base_chain", False, base_deployment_id=base_deployment,
                     failures=["cloudflare returned no canonical production deployment"])
    ok = live_deployment == base_deployment
    return check("base_chain", ok, base_deployment_id=base_deployment,
                 live_canonical_deployment_id=live_deployment,
                 failures=[] if ok else [
                     f"current-content-base.json deployment_id {base_deployment} != live "
                     f"canonical deployment {live_deployment}; the SEO content base was not "
                     "re-minted for the deployment that is actually serving"])


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
#: Checks that read the LIVE edge (Cloudflare Pages) rather than the built tree.
#: Everything else is a pure function of files on disk and cannot be stale.
EDGE_READING_CHECKS = frozenset({"count_copy", "base_chain"})
#: How long to let a fresh deployment settle before believing an edge FAIL.
#:
#: WHY THIS EXISTS (2026-08-18, observed defect)
#: --------------------------------------------
#: Run ~50s after a guarded remint, `count_copy` fetched the six COUNT_COPY_ROUTES
#: and got the PREVIOUS deployment's HTML from the edge, so all six routes
#: reported the old provider count and the sensor returned FAIL with
#: failure_count 6 - for a deployment that was correct. The receipt then said a
#: good deploy was incomplete, which is worse than saying nothing: the wire-in
#: worker treats a non-PASS sensor as a failed deployment.
#:
#: A propagation lag is not a defect, it is physics; the sensor just has to
#: outlast it. So when EVERY failure comes from an edge-reading check, the
#: sensor waits once and re-reads. One retry only - a genuinely wrong deploy
#: still fails, ~75s later, and the receipt records that it waited and what
#: changed, so a real failure can never be mistaken for a settled one.
SETTLE_SECONDS = 75


def run_sensor(release: Path | None = None, offline: bool = False, site: str = SITE,
               settle_seconds: int = SETTLE_SECONDS,
               sleep: Callable[[float], None] = time.sleep,
               live_configuration: bool = False, runtime_root: Path | None = None) -> dict[str, Any]:
    target = release or latest_release()
    rows = registry_rows(target)
    ledger = ledger_entries(target)
    live = live_ids(rows)
    elig = eligibility_ids(target)
    index = research_index()

    def run_checks() -> list[dict[str, Any]]:
        return [
            check_coverage(target, rows, ledger, offline),
            check_surface_completeness(target, rows),
            check_publishable_provider_routes(target, live_configuration=live_configuration or not offline,
                                             runtime_root=runtime_root),
            check_affiliate_surface_tracking(target),
            check_provider_link_monetization(target),
            check_therapeutic_facet_bindings(target, rows),
            check_count_copy(target, rows, site, offline),
            check_derived_tallies(target, rows),
            check_research_binding(live, ledger, index, outside_register_reasons(target, rows), release=target),
            check_freshness_stamps(target),
            check_page_facts(target, live, index),
            check_identity(live, ledger, elig, rows, outside_register_reasons(target, rows)),
            check_identity_dedup(target, rows),
            check_selectable_option_dedup(target),
            check_link_graph(target, rows),
            check_score_binding(target, rows),
            check_aeo_surface(target),
            check_base_chain(offline),
        ]

    checks = run_checks()
    failed = [c for c in checks if c["status"] == "FAIL"]
    settle_retry: dict[str, Any] | None = None
    # One settle-retry: only when the edge is the ONLY thing complaining, and
    # never offline (there is no edge to wait for).
    if failed and not offline and settle_seconds > 0 \
            and all(c["check"] in EDGE_READING_CHECKS for c in failed):
        before = sorted(c["check"] for c in failed)
        sleep(settle_seconds)
        checks = run_checks()
        failed = [c for c in checks if c["status"] == "FAIL"]
        settle_retry = {
            "reason": "every_failure_was_an_edge_reading_check",
            "waited_seconds": settle_seconds,
            "failed_before_settle": before,
            "failed_after_settle": sorted(c["check"] for c in failed),
            "resolved_by_settling": not failed,
        }
    was_skipped = [c for c in checks if c["status"] == "SKIPPED"]
    if failed:
        status = "FAIL"
    elif was_skipped:
        status = "PASS_PARTIAL"
    else:
        status = "PASS"
    all_failures: list[str] = []
    for entry in checks:
        for item in entry.get("failures") or []:
            all_failures.append(f"{entry['check']}: {item}")
        if entry.get("error"):
            all_failures.append(f"{entry['check']}: {entry['error']}")
    return {
        "schema": SENSOR_VERSION,
        "status": status,
        "release": target.name,
        "release_root": str(target),
        "live_programs": len(live),
        "alias_rows": alias_ids(rows),
        "checks": checks,
        "failed_checks": [c["check"] for c in failed],
        "skipped_checks": [c["check"] for c in was_skipped],
        "failure_count": len(all_failures),
        "failures": all_failures,
        "settle_retry": settle_retry,
        "research_roots": [str(p) for p in research_roots()],
        "captured_at_utc": utc(),
        "effects": {"provider_mutations": 0, "deploys": 0, "sheet_writes": 0,
                    "tracking_clicks": 0, "sends": 0},
        "secrets_printed": False,
    }


def write_receipt(result: dict[str, Any], out: Path) -> tuple[str, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    out.write_text(body)
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    return str(out), hashlib.sha256(body.encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", help="release/candidate root to audit (default: newest release)")
    ap.add_argument("--output", help="write the canonical receipt here")
    ap.add_argument("--offline", action="store_true",
                    help="skip network-dependent checks (Cloudflare, live surface audit)")
    ap.add_argument("--site", default=SITE, help=argparse.SUPPRESS)
    ap.add_argument("--mint-aeo-baseline", metavar="BACKLOG_ITEM",
                    help="freeze the release's CURRENT check-#17 defects as accepted-known, "
                         "naming the backlog item that will clear them. Ratchet only: any "
                         "later defect not in the frozen list still fails the deploy.")
    ap.add_argument("--baseline-note", default="")
    ns = ap.parse_args()

    if ns.mint_aeo_baseline:
        if not ns.release:
            print(json.dumps({"status": "ERROR", "error": "release_required_for_baseline_mint"}))
            return 2
        target = Path(ns.release)
        payload = mint_aeo_baseline(target, ns.mint_aeo_baseline, ns.baseline_note)
        out = Path(ns.output) if ns.output else (target / "data/aeo-surface-baseline.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "baseline": str(out),
                          "remediation_item": payload["remediation_item"],
                          "accepted_failure_count": payload["accepted_failure_count"]},
                         indent=2, sort_keys=True))
        return 0

    release = Path(ns.release) if ns.release else None
    if release is not None and not (release / "data/program-registry.json").is_file():
        print(json.dumps({"schema": SENSOR_VERSION, "status": "ERROR",
                          "error": "release_missing_program_registry",
                          "release_root": str(release)}, indent=2, sort_keys=True))
        return 2
    try:
        result = run_sensor(release, offline=ns.offline, site=ns.site)
    except Exception as exc:
        print(json.dumps({"schema": SENSOR_VERSION, "status": "ERROR",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2, sort_keys=True))
        return 2

    out = Path(ns.output) if ns.output else (
        MACHINE_DIR / f"deploy-completeness-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    path, digest = write_receipt(result, out)
    print(json.dumps({
        "status": result["status"],
        "release": result["release"],
        "live_programs": result["live_programs"],
        "failed_checks": result["failed_checks"],
        "skipped_checks": result["skipped_checks"],
        "failure_count": result["failure_count"],
        "failures_sample": result["failures"][:12],
        "receipt": path,
        "receipt_sha256": digest,
    }, indent=2, sort_keys=True))
    return 0 if result["status"] in ("PASS", "PASS_PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
