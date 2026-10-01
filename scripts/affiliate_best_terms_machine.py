#!/usr/bin/env python3
"""Permanent best-collectible affiliate offer machine.

Read-only provider censuses and the canonical Sheet decide. The machine may
stage local switch decisions and queue evidence through the one Sheet writer;
it cannot alter a live link, publish, deploy, join, apply, or mint a link.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

BUSINESS = Path(__file__).resolve().parents[1]
SCRIPTS = BUSINESS / "scripts"
sys.path.insert(0, str(BUSINESS))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(BUSINESS / "ops"))

import affiliate_payout_policy as policy  # noqa: E402
import sheet_drain_invoke  # noqa: E402
import affiliate_sheet_batch_writer as sheet_writer  # noqa: E402
from discord_fmt import lint as lint_discord  # noqa: E402
from discord_fmt import meaning, render, subtext, title  # noqa: E402

BEST_TERMS_CONFIG = BUSINESS / "config/affiliate-sites/best-terms-selection.json"
SELECTION_CONFIG = policy.load_selection_config(BEST_TERMS_CONFIG)
PERFORMANCE_PATH = BUSINESS / str(SELECTION_CONFIG["performance_path"])
COLLECTOR_INTERPRETER = Path(SELECTION_CONFIG["collector_runtime"]["interpreter"])
COLLECTOR_REQUIRED_IMPORTS = tuple(SELECTION_CONFIG["collector_runtime"]["required_imports"])

REPORT_ROOT = (
    BUSINESS / "reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825"
)
RUNS_ROOT = REPORT_ROOT / "runs"
LATEST_MATRIX = REPORT_ROOT / "latest-matrix.json"
LATEST_RUN = REPORT_ROOT / "latest-run.json"
SWITCH_BACKLOG = REPORT_ROOT / "switch-backlog.json"
STATE_DIR = REPORT_ROOT / "state"
APPROVAL_CURSOR = STATE_DIR / "approval-trigger-cursor.json"
FAILURE_STATE = STATE_DIR / "approval-trigger-failure.json"
APPROVAL_REPORT_ROOT = BUSINESS / "reports/glp-affiliate-auto-reconciler"
XENTRAFFIC_BASELINE = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliates/XENTRAFFIC-CATALOG-20260824"
)
PAYOUT_COMPARISONS = XENTRAFFIC_BASELINE / "payout-comparisons.json"
CLINIC_FITS = XENTRAFFIC_BASELINE / "clinic-fits.json"
PAYOUT_MATRIX = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliates/PAYOUT-MATRIX-20260824/payout-matrix.json"
)
SHEET_DRAIN = SCRIPTS / "glp_sheet_sync_drain.py"
SHEET_QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
PAYOUT_LEAK_SOURCE = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/payout-leak-sweep-20260830/"
    "02-leak-quantified.json"
)
PAYOUT_LEAK_QUEUE_RECEIPT = PAYOUT_LEAK_SOURCE.parent / "04-deploy-queue-receipt.json"
PAYOUT_LEAK_DECISION = "STAGE_HIGHER_PAYOUT_RAIL_AT_DEPLOY"
PAYOUT_LEAK_INSTRUCTION_VERSION = "provider-wide-v1"
OPS_ALERTS_CHANNEL = "1487300132054765629"
REQUIRED_MARKETPLACES = (
    "awin", "cj", "rakuten", "partnerstack", "healthtrader", "xentraffic",
    "katalys", "maxbounty", "partnerize", "refersion", "impact", "clickbank",
    "goaffpro", "uppromote", "affiliati",
)
# Standing operator holds apply to census and selection, including old Sheet
# evidence. A hold is an explicit scope exclusion, never an empty catalogue.
HELD_NETWORKS = frozenset({"xentraffic"})
SHEET_LOCK_RC = 75
SHEET_LOCK_RETRY_SECONDS = (3, 8, 20, 40)

ALIASES = {
    "abundrx": "AbundRX",
    "alt rx": "altRx",
    "altrx": "altRx",
    "breeze meds": "Breeze Meds",
    "care bare rx": "Care Bare Rx",
    "curex": "Curex",
    "direct meds": "Direct Meds",
    "eden": "Eden",
    "enhance md": "Enhance.MD",
    "enhanced": "Enhanced",
    "enhanced md": "Enhanced",
    "futurhealth": "FuturHealth",
    "g plan": "G-Plans",
    "g plans": "G-Plans",
    "gala health": "Gala Health",
    "gatlan": "Gatlan",
    "hume health us": "Hume Health",
    "join skinny": "JoinSkinny",
    "joinskinny": "JoinSkinny",
    "joi blokes": "Joi + Blokes",
    "joi and blokes": "Joi + Blokes",
    "jrnys": "JRNYS",
    "levels rx": "LevelsRx",
    "levelsrx": "LevelsRx",
    "lttl": "Lttl",
    "maximus": "Maximus",
    "maximus tribe": "Maximus",
    "medvi": "MEDVI",
    "novi health": "Novi Health",
    "novi": "Novi Health",
    "omzo": "OMZO",
    "refills": "Refills",
    "ro": "Ro",
    "sesame care": "Sesame Care",
    "shedrx": "ShedRX",
    "sunlight rx": "Sunlight",
    "sunlight": "Sunlight",
    "tmates": "TMates",
    "trim rx": "TrimRX",
    "trimrx": "TrimRX",
    "yucca health": "Yucca Health",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def weekly_window_id(moment: dt.datetime | None = None) -> str:
    value = (moment or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def claim_weekly_window(
    state_dir: Path | None = None, *, moment: dt.datetime | None = None,
) -> dict[str, Any]:
    """Atomically permit at most one weekly machine run per ISO-week window."""
    state_dir = Path(state_dir or STATE_DIR)
    state_dir.mkdir(parents=True, exist_ok=True)
    window = weekly_window_id(moment)
    marker = state_dir / f"weekly-window-{window}.json"
    lock = state_dir / ".weekly-window.lock"
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        existing = read_json(marker, None)
        if isinstance(existing, dict):
            fcntl.flock(handle, fcntl.LOCK_UN)
            return {"claimed": False, "duplicate": True, "window_id": window,
                    "marker": str(marker), "existing": existing}
        claimed_at = (moment or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        payload = {
            "schema": "glp.best-terms-weekly-window.v1",
            "window_id": window,
            "claimed_at_utc": claimed_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        atomic_json(marker, payload)
        fcntl.flock(handle, fcntl.LOCK_UN)
    return {"claimed": True, "duplicate": False, "window_id": window,
            "marker": str(marker), "existing": None}


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "unknown"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def repo_rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(BUSINESS))
    except ValueError:
        return str(path)


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def load_performance_rows(path: Path = PERFORMANCE_PATH) -> list[dict[str, Any]]:
    document = read_json(path, None)
    if not isinstance(document, dict):
        raise RuntimeError(f"affiliate_payout_performance_unreadable:{path}")
    if document.get("schema") != "glp.affiliate-payout-performance.v1":
        raise ValueError("affiliate_payout_performance_schema_invalid")
    rows = document.get("records")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("affiliate_payout_performance_records_invalid")
    return rows


def collector_runtime_preflight(
    interpreter: Path = COLLECTOR_INTERPRETER,
    required_imports: Iterable[str] = COLLECTOR_REQUIRED_IMPORTS,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Prove the configured collector can import every dependency before launch."""
    runner = runner or subprocess.run
    interpreter = Path(interpreter)
    imports = tuple(str(name) for name in required_imports)
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise RuntimeError(f"collector_interpreter_unavailable:{interpreter}")
    if not imports:
        raise RuntimeError("collector_required_imports_empty")
    probe = (
        "import importlib, json, sys; "
        "[importlib.import_module(name) for name in json.loads(sys.argv[1])]"
    )
    completed = runner(
        [str(interpreter), "-c", probe, json.dumps(imports)],
        cwd=BUSINESS, capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "import probe failed")[-500:]
        raise RuntimeError(f"collector_dependency_preflight_failed:{detail}")
    return {"status": "PASS", "interpreter": str(interpreter),
            "required_imports": list(imports)}


def terms_authority(record: dict[str, Any]) -> str:
    """Classify commercial-term provenance; API is always strongest."""
    text = " ".join(str(record.get(field) or "") for field in (
        "terms_source", "source_authority", "source_type", "source",
        "payout_parse_basis",
    )).casefold()
    if "api" in text or "authenticated_adapter" in text or "everflow" in text:
        return "api"
    if "sheet" in text:
        return "sheet"
    if "portal" in text or "browser" in text:
        return "portal"
    if "email" in text or "inbox" in text:
        return "email"
    return "unknown"


def contradiction_risk_acceptance(
    contradictions: list[dict[str, Any]], *, now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    if not contradictions:
        return None
    if not any(row.get("authoritative_source") == "api" for row in contradictions):
        return None
    accepted = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    hours = int((SELECTION_CONFIG.get("contradiction_policy") or {}).get("recheck_hours") or 48)
    recheck = accepted + dt.timedelta(hours=hours)
    return {
        "status": "ACCEPT_API_TRUTH_PENDING_RECHECK",
        "authority_rule": "api_out_ranks_portal_and_email",
        "accepted_at_utc": accepted.isoformat().replace("+00:00", "Z"),
        "recheck_after_utc": recheck.isoformat().replace("+00:00", "Z"),
        "recheck_window_hours": hours,
        "contradiction_count": len(contradictions),
    }


def truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {"true", "yes", "1"}


def clean_provider_name(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s*[|—-]\s*(?:GLP-?1 Weight Loss|XenTraffic|Katalys|Affiliati).*$", "", text, flags=re.I)
    text = re.sub(r"\s*\((?:XenTraffic|Katalys|Awin|MaxBounty|Affiliati)[^)]*\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\[(?:XenTraffic|Katalys|Awin|MaxBounty|Affiliati)[^]]*\]\s*$", "", text, flags=re.I)
    key = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    if key in ALIASES:
        return ALIASES[key]
    for alias, canonical in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if key.startswith(alias + " "):
            return canonical
    return text


def program_id_from_row(row: list[Any]) -> str:
    network = str(row[4] or "")
    text = " ".join(str(row[index] or "") for index in (5, 14, 22, 23))
    patterns = []
    if policy.network_key(network) == "roeverflowwhitelabeltenant4131":
        match = re.fullmatch(r"https://ro\.everflowclient\.io/offers/([0-9]+)", str(row[5] or ""))
        return match.group(1) if match else ""
    if policy.network_key(network) == "xentraffic":
        patterns.extend((r"/offers/(\d+)", r"\boffer\s*(?:id)?\s*[:#-]?\s*(\d+)"))
    elif policy.network_key(network) == "katalys":
        patterns.extend((r"/explore/(\d+)", r"Katalys (?:offer|programme)\s*(\d+)"))
    elif policy.network_key(network) == "awin":
        patterns.extend((r"merchant-profile/(\d+)", r"(?:awinmid|merchantId|advertiserId)=(\d+)"))
    elif policy.network_key(network) == "cj":
        patterns.append(r"advertiserId[=:/](\d+)")
    elif policy.network_key(network) == "impact":
        patterns.extend((
            r"/Campaigns?/(\d+)",
            r"Impact campaign\s*(\d+)",
            # Impact tracking links end in /<campaign_id>. The governed wire-in
            # note can replace the original approval note, so the link itself is
            # the durable immutable-id source after that rewrite.
            r"pxf\.io/(?:[^/?#]+/){3}(\d+)(?=[\s?#]|$)",
        ))
    elif policy.network_key(network) == "maxbounty":
        patterns.extend((r"/campaigns?/(\d+)", r"campaign\s*(\d+)", r"/(\d{3,8})/?(?:[?#]|$)"))
    elif policy.network_key(network) == "affiliati":
        patterns.extend((
            r"Affiliati (?:offer|programme|program)\s*(\d+)",
            r"\boffer\s*(?:id)?\s*[:#-]?\s*(\d+)",
            # The canonical Sheet row keeps Affiliati's offer id in the
            # governed terms-capture filename even after its generic portal
            # URL is replaced by the wire-in note.  The separate immutable
            # tracking-link id is not the commercial program identity.
            r"affiliati-(\d+)-terms-verbatim\.json",
        ))
    patterns.append(r"provider_program_id[=: ]+(\w[\w.-]+)")
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


def parse_payout(text: Any) -> dict[str, Any]:
    raw = str(text or "").strip()
    low = raw.casefold()
    explicit_event = re.search(r"(?:^|;)\s*paid conversion event:\s*(.+?)\s*$", raw, re.I)
    payout_text = raw[:explicit_event.start()].strip(" ;") if explicit_event else raw
    amounts = []
    for value in re.findall(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", payout_text):
        number = float(value.replace(",", ""))
        if number not in amounts:
            amounts.append(number)
    if explicit_event:
        paid_event = explicit_event.group(1).strip()
        paid_event_basis = "explicit_paid_conversion_event"
    elif re.search(r"\bglp[- ]?1\b", low) and ("cpa" in low or "acquisition" in low):
        paid_event, paid_event_basis = "glp1_acquisition", "explicit_glp1_cpa"
    elif "medication subscription signup" in low:
        paid_event, paid_event_basis = (
            "medication_subscription_signup", "explicit_medication_subscription_signup")
    elif "appointment booked" in low or "booked appointment" in low:
        paid_event, paid_event_basis = "appointment_booked", "explicit_appointment_booked"
    elif "qualified lead" in low:
        paid_event, paid_event_basis = "qualified_lead", "explicit_qualified_lead"
    elif re.search(r"\bconverts?\s+on\s+(?:initial\s+)?(?:credit\s+card\s+transaction\s*/?\s*)?purchase\b", low):
        paid_event, paid_event_basis = "purchase", "explicit_converts_on_purchase"
    elif re.search(r"\bper\s+sale\b", low):
        paid_event, paid_event_basis = "sale", "explicit_per_sale"
    elif re.search(r"\bper\s+(?:purchase|order)\b", low):
        paid_event, paid_event_basis = "purchase", "explicit_per_purchase"
    else:
        paid_event, paid_event_basis = "", "payout_model_fallback"
    if "%" in raw or "revshare" in low or "revenue share" in low:
        percentages = re.findall(r"([0-9]+(?:\.\d+)?)\s*%", raw)
        return {
            "headline_payout": float(percentages[0]) if len(set(percentages)) == 1 else None,
            "payout_model": "RevShare",
            "payout_display": raw,
            "payout_parse_basis": "percentage_retained_without_cpa_conversion",
            "paid_event": paid_event,
            "paid_event_basis": paid_event_basis,
        }
    if "cpl" in low or "per lead" in low or "qualified lead" in low:
        model = "CPL"
    elif "cps" in low or "per sale" in low:
        model = "CPS"
    elif ("cpa" in low or "per acquisition" in low or "per conversion" in low
          or "medication subscription signup" in low or "appointment booked" in low):
        model = "CPA"
    elif "fixed" in low and "lead" in low:
        model = "CPL"
    elif "fixed" in low:
        model = "CPA"
    else:
        model = "unknown"
    return {
        "headline_payout": amounts[0] if len(amounts) == 1 else None,
        "payout_model": model,
        "payout_display": raw,
        "payout_parse_basis": "one_unambiguous_dollar_amount" if len(amounts) == 1 else "not_normalized_ambiguous_or_missing",
        "paid_event": paid_event,
        "paid_event_basis": paid_event_basis,
    }


def sheet_relationship(status: Any) -> str:
    low = str(status or "").strip().casefold()
    if low.startswith("approved_") or low in {"active", "joined", "live", "live_wired_verified"}:
        return "approved"
    if "rejected" in low or "declined" in low or "denied" in low:
        return "rejected"
    if "not" in low and ("applied" in low or "joined" in low):
        return "notjoined"
    if "pending" in low or "submitted" in low or "unapproved" in low or "applied" in low:
        return "pending"
    return low or "unknown"


def read_live_sheet() -> list[list[Any]]:
    from googleapiclient.discovery import build  # noqa: PLC0415
    import glp_sheet_sync_drain as drain  # noqa: PLC0415

    svc = build("sheets", "v4", credentials=sheet_writer.creds(), cache_discovery=False)
    return drain.read_sheet(svc)


def payout_matrix_evidence() -> dict[tuple[str, str], dict[str, Any]]:
    """Index authenticated commercial facts without reviving stale approval state."""
    document = read_json(PAYOUT_MATRIX, {})
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    for provider in document.get("providers") or []:
        for offer in provider.get("all_marketplace_offers") or []:
            network = policy.network_key(offer.get("network"))
            program_id = str(offer.get("program_id") or "")
            if network and program_id:
                evidence[(network, program_id)] = offer
    return evidence


def sheet_offers(values: list[list[Any]]) -> list[dict[str, Any]]:
    offers = []
    commercial_evidence = payout_matrix_evidence()
    for row_number, raw in enumerate(values[1:], 2):
        row = sheet_writer.pad(raw)
        program = str(row[2] or "").strip()
        network = policy.network_key(row[4])
        if not program or network not in policy.NETWORKS:
            continue
        provider = clean_provider_name(program)
        status = str(row[10] or "")
        relationship = sheet_relationship(status)
        payout = parse_payout(row[16])
        if not payout["paid_event"]:
            sheet_context = " | ".join((str(row[22] or ""), str(row[23] or "").split("[PAYOUT POLICY]", 1)[0]))
            # Several XenTraffic rows retained Katalys compliance prose from an
            # older cross-rail projection. It is useful traffic-policy context,
            # but cannot prove XenTraffic's paid event. Never promote a clearly
            # foreign-rail label into same-event payout comparability.
            foreign_event_evidence = (
                network == "xentraffic" and "katalys prohibited:" in sheet_context.casefold()
            )
            event_match = None if foreign_event_evidence else re.search(
                r"\bconversion\s+event\s*:\s*(purchase|sale|order|qualified\s+lead|"
                r"subscription(?:\s+signup)?|appointment(?:\s+booked)?)\b",
                sheet_context,
                re.I,
            )
            if event_match:
                payout["paid_event"] = " ".join(event_match.group(1).casefold().split())
                payout["paid_event_basis"] = "explicit_sheet_conversion_event"
        program_id = program_id_from_row(row)
        prior = commercial_evidence.get((network, program_id)) or {}
        # Marketplace display names routinely append offer mechanics instead of
        # the provider identity (for example `Gala - GLP-1 - CPS (US)`).  When
        # the authenticated payout matrix has already bound this exact
        # network+program id to a canonical provider, reuse that identity so all
        # rails enter one provider group.  This does not revive relationship or
        # wire state from the matrix; those remain live-Sheet-only below.
        prior_provider = clean_provider_name(
            prior.get("provider") or prior.get("provider_name") or ""
        )
        if prior_provider:
            provider = prior_provider
        if payout["payout_model"] == "unknown" and prior.get("payout_model"):
            payout["payout_model"] = str(prior["payout_model"])
            payout["payout_parse_basis"] += "+authenticated_prior_model"
        if payout["headline_payout"] is None and prior.get("headline_value") is not None:
            payout["headline_payout"] = prior["headline_value"]
            payout["payout_parse_basis"] += "+authenticated_prior_amount"
        link = str(row[14] or "").strip()
        link_qa = str(row[21] or "").strip().casefold()
        evidence_ready = bool(
            relationship == "approved" and link.startswith("https://")
            and link_qa not in {"", "unverified", "not_checked", "link_revalidation_required_after_reactivation"}
        )
        wire_ready = truthy(row[19]) or truthy(row[20]) or evidence_ready
        # Prior payout matrices are commercial evidence only. They must never
        # revive a current Sheet row whose retained link is explicitly waiting
        # for revalidation (or otherwise replace the current wire gate).
        if link_qa == "link_revalidation_required_after_reactivation":
            wire_ready = False
        # MaxBounty has a standing site/image rollout gate. An approved account is
        # useful business context, but it cannot silently become a winner.
        if network == "maxbounty" and not (truthy(row[19]) or truthy(row[20])):
            wire_ready = False
        offer = {
            "provider_name": provider,
            # Merchant destinations are stronger cross-rail identity evidence
            # than decorated marketplace names. The policy rejects rail hosts
            # and never changes provider x marketplace Sheet row identity.
            "official_url": str(row[6] or "").strip(),
            "network": network,
            "program_id": program_id,
            "headline_payout": payout["headline_payout"],
            "payout_model": payout["payout_model"],
            "payout_display": payout["payout_display"],
            "payout_parse_basis": payout["payout_parse_basis"],
            "paid_event": payout["paid_event"],
            "paid_event_basis": payout["paid_event_basis"],
            "currency": "USD",
            "relationship": relationship,
            "wire_in_ready": wire_ready,
            "wired_live": truthy(row[20]),
            "tracking_link_present": link.startswith("https://"),
            "attribution_days": prior.get("attribution_days"),
            "sheet_row": row_number,
            "source": (
                "live_sheet+payout_matrix_commercial_evidence"
                if prior else "live_sheet"
            ),
            "terms_source": "sheet",
            "status": status,
            "standing_gate": (
                "maxbounty_site_deployment_plus_operator_approved_advertiser_display_image"
                if network == "maxbounty" and not wire_ready else None
            ),
            "traffic_eligible": False if provider.casefold() == "fella health" else True,
            "permanently_excluded": provider.casefold() == "fella health",
        }
        offers.append(offer)
    return offers


def latest_xentraffic_catalog(explicit: Path | None = None) -> Path:
    candidates = []
    if explicit:
        if explicit.is_file():
            candidates.append(explicit)
        else:
            candidates.extend((explicit / "clinic-fits.json", explicit / "catalog-normalized.json"))
    candidates.extend((
        XENTRAFFIC_BASELINE / "clinic-fits.json",
        XENTRAFFIC_BASELINE / "catalog-normalized.json",
    ))
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("xentraffic_catalog_missing")


def xentraffic_offers(path: Path) -> list[dict[str, Any]]:
    document = read_json(path, {})
    raw_offers = document.get("offers") or []
    if document.get("providers") and not raw_offers:
        raw_offers = [
            {**offer, "clinic_provider": row.get("provider")}
            for row in document.get("providers") or []
            for offer in row.get("offers") or []
        ]
    out = []
    for raw in raw_offers:
        if raw.get("clinic_scout_fit") is False:
            continue
        verdict = str(raw.get("clinic_fit_verdict") or raw.get("fit_verdict") or "")
        if verdict and verdict not in {
            "core_glp1", "sister_vertical:medical_weight_loss",
            "core_clinic", "clinic_fit", "glp-clinic-scout",
        }:
            continue
        name = str(
            raw.get("clinic_provider") or raw.get("name")
            or raw.get("advertiser_name") or raw.get("offer_name") or ""
        ).strip()
        provider = clean_provider_name(name)
        payout_type = str(
            raw.get("payout_type") or raw.get("commercial_trigger")
            or raw.get("payout_model") or ""
        ).strip().upper()
        payout_model = {"CPL": "CPL", "CPA": "CPA", "CPS": "CPS"}.get(payout_type, payout_type or "unknown")
        amount = raw.get("payout_amount")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = None
        tracking = str(raw.get("tracking_url") or raw.get("tracking_link") or "")
        approval = raw.get("approval_state") if isinstance(raw.get("approval_state"), dict) else {}
        relationship = str(
            raw.get("relationship") or approval.get("affiliate_status") or "active"
        ).casefold()
        if relationship == "public":
            relationship = "active"
        runnable = bool(
            tracking.startswith("https://")
            or (
                approval.get("runnable") is True
                and str(approval.get("offer_status") or "").casefold() == "active"
            )
        )
        out.append({
            "provider_name": provider,
            "official_url": "",
            "network": "xentraffic",
            "program_id": str(raw.get("offer_id") or raw.get("program_id") or ""),
            "headline_payout": amount,
            "payout_model": payout_model,
            "payout_display": str(
                raw.get("payout_display")
                or (f"{payout_model}: ${amount:g}" if amount is not None else "")
            ),
            "payout_parse_basis": "authenticated_xentraffic_offer_detail",
            "currency": str(raw.get("currency") or "USD").upper(),
            "relationship": relationship if relationship in {"active", "approved"} else "active",
            "wire_in_ready": runnable,
            "wired_live": False,
            "tracking_link_present": tracking.startswith("https://"),
            "tracking_link": tracking,
            "attribution_days": raw.get("attribution_days"),
            "source": repo_rel(path),
            "terms_source": "api",
            "status": str(raw.get("status") or "public"),
            "traffic_eligible": provider.casefold() != "fella health",
            "permanently_excluded": provider.casefold() == "fella health",
        })
    return out


def reconcile_xentraffic_availability(offers: list[dict[str, Any]], catalog: Path) -> None:
    document = read_json(catalog, {})
    if document.get("catalog_path"):
        catalog = BUSINESS / document["catalog_path"]
        document = read_json(catalog, {})
    complete = supplied_xentraffic_summary(catalog)
    if complete["completeness"] != "COMPLETE":
        raise RuntimeError("xentraffic_availability_requires_complete_catalog")
    ids = {str(row.get("offer_id") or row.get("program_id") or "")
           for row in document.get("offers") or []}
    if "" in ids or len(ids) != complete["enumerated"]:
        raise RuntimeError("xentraffic_availability_identity_count_mismatch")
    for offer in offers:
        if offer.get("network") == "xentraffic" and str(offer.get("program_id")) not in ids:
            # Retain historical payout/relationship evidence, but do not turn
            # an old Sheet row into current account access or a payable winner.
            offer["historical_relationship"] = offer.get("relationship")
            offer["relationship"] = "unknown"
            offer["wire_in_ready"] = False
            offer["availability"] = "not_in_current_authenticated_catalog"
            offer["availability_source"] = repo_rel(catalog)


def application_ledger_offers() -> list[dict[str, Any]]:
    path = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/applications-ledger.json"
    document = read_json(path, {})
    records = document.get("applications") or document.get("records") or document.get("programs") or []
    out = []
    for raw in records:
        provider = clean_provider_name(raw.get("name") or raw.get("program_name"))
        if not provider:
            continue
        relationship = str(raw.get("current_relationship") or raw.get("relationship") or "unknown")
        out.append({
            "provider_name": provider,
            "official_url": "",
            "network": policy.network_key(raw.get("network")),
            "program_id": str(raw.get("program_id") or ""),
            "headline_payout": None,
            "payout_model": "unknown",
            "payout_display": "",
            "payout_parse_basis": "application_ledger_has_no_payout",
            "currency": "USD",
            "relationship": relationship,
            # Ledger holds are independent of commercial/source precedence.
            # Preserve them before a stale Sheet alias of this exact rail merges.
            "offer_status": raw.get("offer_status"),
            "network_status": raw.get("network_status"),
            "held": raw.get("held"),
            "suppressed": raw.get("suppressed"),
            "source_exclusion_reason": offer_exclusion_reason(raw),
            "wire_in_ready": False,
            "wired_live": False,
            "tracking_link_present": False,
            "attribution_days": None,
            "source": repo_rel(path),
            "terms_source": "email",
            "status": str(raw.get("status") or ""),
            "traffic_eligible": True,
        })
    return out


def inventory_programs(census_dir: Path | None) -> list[dict[str, Any]]:
    if not census_dir or not census_dir.is_dir():
        return []
    out = []
    # Collectors publish both <network>.json and normalized inventories.
    # Require an actual program list so summary/receipt files are not offers.
    for path in sorted(census_dir.glob("*.json")):
        document = read_json(path, {})
        if not isinstance(document, dict) or not isinstance(document.get("programs"), list):
            continue
        programs = document["programs"]
        for raw in programs:
            provider = clean_provider_name(raw.get("name") or raw.get("program_name"))
            if not provider:
                continue
            payout = parse_payout(raw.get("commission") or raw.get("payout") or "")
            # API-backed adapters can provide the commercial tuple directly.
            # Preserve it verbatim rather than reparsing prose and losing the
            # conversion event that makes cross-rail comparison honest.
            if raw.get("headline_payout") is not None:
                payout["headline_payout"] = raw.get("headline_payout")
                payout["payout_parse_basis"] = "authenticated_adapter_structured_payout"
            for field in ("payout_model", "payout_display", "paid_event", "paid_event_basis"):
                if raw.get(field) not in (None, ""):
                    payout[field] = raw.get(field)
            out.append({
                "provider_name": provider,
                "official_url": "",
                "network": policy.network_key(document.get("network") or path.stem.replace("-inventory", "")),
                "program_id": str(raw.get("program_id") or raw.get("id") or ""),
                "headline_payout": payout["headline_payout"],
                "payout_model": payout["payout_model"],
                "payout_display": payout["payout_display"],
                "payout_parse_basis": payout["payout_parse_basis"],
                "paid_event": payout.get("paid_event") or "",
                "paid_event_basis": payout.get("paid_event_basis") or "",
                "currency": str(raw.get("currency") or "USD").upper(),
                "relationship": str(raw.get("relationship") or "unknown"),
                "held": raw.get("held"),
                "suppressed": raw.get("suppressed"),
                "source_exclusion_reason": offer_exclusion_reason(raw),
                "wire_in_ready": False,
                "wired_live": False,
                "tracking_link_present": False,
                "attribution_days": raw.get("attribution_days"),
                "source": repo_rel(path),
                "terms_source": str(
                    raw.get("terms_source") or raw.get("source_authority")
                    or document.get("terms_source") or document.get("source_authority")
                    or ("api" if payout["payout_parse_basis"] == "authenticated_adapter_structured_payout"
                        else "portal")
                ).casefold(),
                "status": str(raw.get("status") or ""),
                "traffic_eligible": True,
            })
    return out


def merge_offers(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    authority_order = list(
        (SELECTION_CONFIG.get("source_authority") or {}).get("order_high_to_low")
        or ["api", "sheet", "portal", "email", "unknown"]
    )
    authority_rank = {
        name: len(authority_order) - index for index, name in enumerate(authority_order)
    }
    commercial_fields = (
        "headline_payout", "payout_model", "payout_display", "payout_parse_basis",
        "paid_event", "paid_event_basis", "currency", "attribution_days",
    )
    contradiction_fields = {
        "headline_payout", "payout_model", "paid_event", "currency", "attribution_days",
    }
    # Negative eligibility evidence is independent of commercial precedence and
    # provider labels. Freeze it before a positive source can overwrite fields.
    records = list(records)
    blocked = excluded_rail_evidence(records)
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in policy.resolve_provider_records(records):
        provider = clean_provider_name(row.get("provider_name"))
        row["provider_name"] = provider
        identity = str(row.get("provider_identity") or policy.provider_identity(row))
        key = (identity, policy.network_key(row.get("network")), str(row.get("program_id") or ""))
        incoming_authority = terms_authority(row)
        if key in merged:
            current = merged[key]
            sources = set(current.get("sources") or [current.get("source")])
            sources.add(row.get("source"))
            field_authority = current.setdefault("_commercial_field_authority", {})
            contradictions = current.setdefault("terms_contradictions", [])
            for field in commercial_fields:
                incoming_value = row.get(field)
                if incoming_value in (None, "", "unknown"):
                    continue
                current_value = current.get(field)
                current_authority = str(field_authority.get(field) or terms_authority(current))
                if current_value in (None, "", "unknown"):
                    current[field] = row[field]
                    field_authority[field] = incoming_authority
                    continue
                differs = (
                    str(current_value).strip().casefold()
                    != str(incoming_value).strip().casefold()
                )
                incoming_wins = (
                    authority_rank.get(incoming_authority, 0)
                    > authority_rank.get(current_authority, 0)
                )
                if differs and field in contradiction_fields and (
                    "api" in {current_authority, incoming_authority}
                    or {current_authority, incoming_authority} & {"portal", "email"}
                ):
                    authoritative_source = incoming_authority if incoming_wins else current_authority
                    authoritative_value = incoming_value if incoming_wins else current_value
                    conflicting_source = current_authority if incoming_wins else incoming_authority
                    conflicting_value = current_value if incoming_wins else incoming_value
                    contradiction = {
                        "field": field,
                        "authoritative_source": authoritative_source,
                        "authoritative_value": authoritative_value,
                        "conflicting_source": conflicting_source,
                        "conflicting_value": conflicting_value,
                    }
                    if contradiction not in contradictions:
                        contradictions.append(contradiction)
                if incoming_wins:
                    current[field] = incoming_value
                    field_authority[field] = incoming_authority
            current["wire_in_ready"] = bool(current.get("wire_in_ready") or row.get("wire_in_ready"))
            current["wired_live"] = bool(current.get("wired_live") or row.get("wired_live"))
            current["tracking_link_present"] = bool(current.get("tracking_link_present") or row.get("tracking_link_present"))
            if row.get("tracking_link"):
                current["tracking_link"] = row["tracking_link"]
            if row.get("sheet_row"):
                current["sheet_row"] = row["sheet_row"]
            # Input order is authority order: live Sheet, authenticated Xen
            # offer detail, application ledger, then broad marketplace census.
            # A later catalogue row must not turn a pending application into an
            # approval. Approval is recorded through the inbox/Sheet path first.
            current["sources"] = sorted(str(source) for source in sources if source)
            strongest = max(
                set(field_authority.values()) | {terms_authority(current), incoming_authority},
                key=lambda name: authority_rank.get(name, 0),
            )
            current["terms_authority"] = strongest
            current["terms_source"] = strongest
            risk = contradiction_risk_acceptance(contradictions)
            if risk:
                current["risk_acceptance"] = risk
        else:
            row["network"] = policy.network_key(row.get("network"))
            row["sources"] = [str(row.get("source"))] if row.get("source") else []
            row["terms_authority"] = incoming_authority
            row["_commercial_field_authority"] = {
                field: incoming_authority for field in commercial_fields
                if row.get(field) not in (None, "", "unknown")
            }
            merged[key] = row
    out = sorted(merged.values(), key=lambda row: (
        slug(row["provider_name"]), row["network"], str(row.get("program_id") or "")))
    for row in out:
        if evidence := blocked.get(offer_rail_identity(row)):
            row.update(evidence)
        row.pop("_commercial_field_authority", None)
        if not row.get("terms_contradictions"):
            row.pop("terms_contradictions", None)
    return out


def run_supply_census(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    collector = SCRIPTS / "marketplace_supply_sweep.py"
    if not collector.is_file():
        raise RuntimeError(f"best_terms_collector_source_missing:{collector}")
    runtime = collector_runtime_preflight()
    census_dir = run_dir / "census"
    command = [str(COLLECTOR_INTERPRETER), str(collector),
               "--outdir", str(census_dir), "--only",
               *[name for name in REQUIRED_MARKETPLACES if name not in HELD_NETWORKS]]
    completed = subprocess.run(command, cwd=BUSINESS, capture_output=True, text=True, timeout=1800)
    summary = read_json(census_dir / "_sweep-summary.json", {})
    if completed.returncode != 0 or not summary:
        detail = str(completed.stderr or completed.stdout or "summary missing")[-500:]
        raise RuntimeError(
            f"marketplace_supply_sweep_failed_loud:rc={completed.returncode}:{detail}"
        )
    summary["collector_runtime_preflight"] = runtime
    return census_dir, summary


def run_xentraffic_census(run_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    runtime = collector_runtime_preflight()
    output = run_dir / "xentraffic-census"
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "run-summary.json"
    command = [str(COLLECTOR_INTERPRETER), str(SCRIPTS / "glp_xentraffic_enumerate.py"),
               "--detail", "candidates", "--queue", "off", "--output", str(summary_path)]
    completed = subprocess.run(command, cwd=BUSINESS, capture_output=True, text=True, timeout=1800)
    payload = read_json(summary_path, {})
    summary = payload.get("summary") or {}
    capture_path = str(payload.get("capture_path") or "")
    catalog = BUSINESS / capture_path if capture_path else output / "missing-offers.json"
    if (
        completed.returncode == 0
        and catalog.is_file()
        and payload.get("status") == "PASS"
        and summary.get("completeness") == "COMPLETE"
    ):
        return catalog, {
            "completeness": str(summary.get("completeness")),
            "detail": str(summary.get("note") or "authenticated XenTraffic catalogue enumerated"),
            "enumerated": summary.get("offers"),
            "reported_total": summary.get("reported_total"),
            "auth_state": "PASS",
            "collector_runtime_preflight": runtime,
            "receipt": repo_rel(summary_path),
            "capture": repo_rel(catalog),
        }
    detail = str(
        payload.get("reason") or summary.get("note")
        or completed.stderr or completed.stdout or "run-summary unavailable"
    )[:500]
    raise RuntimeError(f"xentraffic_census_failed_loud:rc={completed.returncode}:{detail}")


def supplied_xentraffic_summary(path: Path) -> dict[str, Any]:
    """Prove a supplied clinic-fit slice from its linked full census denominator."""
    selection = read_json(path, {})
    source_path = path
    linked = str(selection.get("catalog_path") or "").strip()
    if linked:
        candidate = BUSINESS / linked
        if candidate.is_file():
            source_path = candidate
    source = read_json(source_path, {})
    completeness = source.get("completeness") or {}
    enumerated = completeness.get("enumerated")
    reported = completeness.get("reported_total")
    proven = bool(
        str(completeness.get("verdict") or "").upper() == "COMPLETE"
        and isinstance(enumerated, int)
        and isinstance(reported, int)
        and enumerated > 0
        and enumerated == reported
    )
    return {
        "completeness": "COMPLETE" if proven else "NO_PROVEN_DENOMINATOR",
        "detail": (
            f"authenticated XenTraffic source catalogue supplied; {enumerated}/{reported}"
            if proven else "supplied XenTraffic catalogue has no proven complete denominator"
        ),
        "enumerated": enumerated,
        "reported_total": reported,
        "auth_state": "PASS_FROM_CAPTURE" if proven else "PARTIAL_FROM_CAPTURE",
        "receipt": repo_rel(source_path),
        "selection_receipt": repo_rel(path),
    }


def census_contract(summary: dict[str, Any], xen: dict[str, Any]) -> dict[str, Any]:
    networks = dict(summary.get("networks") or {})
    networks["xentraffic"] = xen
    failures = []
    for network in REQUIRED_MARKETPLACES:
        if network in HELD_NETWORKS:
            networks[network] = held_source_summary(network)
            continue
        row = networks.get(network)
        if network == "affiliati" and row and row.get("auth_state") == "BLOCKED":
            row = {**row, "completeness": "NOT_YET_APPROVED",
                   "detail": "excluded from authenticated inputs until marketplace approval is held"}
            networks[network] = row
            continue
        if not row:
            failures.append({"network": network, "failing_step": "collector_not_registered",
                             "detail": "no census result exists"})
            continue
        if str(row.get("completeness")) != "COMPLETE":
            failures.append({
                "network": network,
                "failing_step": str(row.get("completeness") or "unknown"),
                "detail": str(row.get("detail") or "")[:300],
                "receipt": row.get("receipt"),
            })
    return {
        "schema": "glp.best-terms-census-contract.v1",
        "status": "PASS" if not failures else "PARTIAL",
        "denominator_proven": not failures,
        "required_marketplaces": list(REQUIRED_MARKETPLACES),
        "denominator_scope": "eligible_nonheld_marketplaces_only",
        "excluded_marketplaces": sorted(HELD_NETWORKS),
        "networks": networks,
        "failures": failures,
    }


def held_source_summary(network: str) -> dict[str, Any]:
    return {"completeness": "EXCLUDED_OPERATOR_HOLD", "auth_state": "NOT_ATTEMPTED",
            "detail": f"Standing operator hold: {network}; no catalogue or terms used",
            "enumerated": None, "reported_total": None}


def offer_exclusion_reason(offer: dict[str, Any]) -> str | None:
    if policy.network_key(offer.get("network")) in HELD_NETWORKS:
        return "operator_network_hold"
    name = policy.provider_name_key(offer.get("provider_name") or offer.get("provider"))
    if name.startswith(("fellahealth",)) or name == "fella":
        return "permanent_provider_exclusion"
    if name.startswith(("telos", "healthrx")):
        return "operator_provider_hold"
    if offer.get("source_exclusion_reason"):
        return str(offer["source_exclusion_reason"])
    if any(offer.get(k) not in (None, False, "", "false", "FALSE", "0", 0)
           for k in ("held", "suppressed")):
        return "source_hold_or_suppression"
    if any(re.search(r"(?:^|[^a-z])(hold|held|suppressed)(?:$|[^a-z])",
                     str(offer.get(k) or "").casefold())
           for k in ("relationship", "status", "offer_status", "network_status")):
        return "source_hold_or_suppression"
    return None


def offer_rail_identity(offer: dict[str, Any]) -> tuple[str, str] | None:
    network = policy.network_key(offer.get("network"))
    program_id = str(offer.get("program_id") or "").strip()
    return (network, program_id) if network and program_id else None


def excluded_rail_evidence(offers: Iterable[dict[str, Any]]) -> dict:
    blocked: dict = {}
    for row in offers:
        key, reason = offer_rail_identity(row), offer_exclusion_reason(row)
        if key and reason:
            evidence = blocked.setdefault(key, {"source_exclusion_reason": reason})
            for flag in ("held", "suppressed"):
                if row.get(flag) not in (None, False, "", "false", "FALSE", "0", 0):
                    evidence[flag] = True
    return blocked


def selection_with_offers(
    offers: list[dict[str, Any]],
    performance_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Recheck exact rail identity here even for callers that bypass the merge.
    blocked = excluded_rail_evidence(offers)
    offers = [{**row, **blocked.get(offer_rail_identity(row), {})} for row in offers]
    excluded = [{"provider_name": row.get("provider_name"), "network": row.get("network"),
                 "program_id": row.get("program_id"), "reason": reason}
                for row in offers if (reason := offer_exclusion_reason(row))]
    offers = [row for row in offers if not offer_exclusion_reason(row)]
    measured = load_performance_rows() if performance_rows is None else list(performance_rows)
    selection = policy.select_best_collectible(
        offers, measured, selection_config=SELECTION_CONFIG)
    selection["excluded_offers"] = excluded
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for offer in policy.resolve_provider_records(offers):
        by_provider.setdefault(str(offer.get("provider_identity") or ""), []).append(offer)
    for provider in selection["providers"]:
        provider["offers"] = by_provider.get(str(provider.get("provider_identity") or ""), [])
        # Broad catalogues can contain mojibake or decorated marketplace names
        # before the canonical Sheet row in sort order (for example `Gaâla`).
        # Prefer the proved live/Sheet merchant row for the public provider
        # label without changing provider x marketplace identities.
        if provider["offers"]:
            canonical = max(
                provider["offers"],
                key=lambda row: (
                    bool(row.get("wired_live")),
                    bool(row.get("sheet_row")),
                    bool(row.get("official_url")),
                    bool(row.get("wire_in_ready")),
                ),
            )
            provider["provider_name"] = clean_provider_name(
                canonical.get("provider_name") or provider.get("provider_name")
            )
        provider["provider_key"] = str(provider.get("provider_identity") or "")
        provider["active_routes"] = [
            {"network": row["network"], "program_id": str(row.get("program_id") or ""),
             "payout_display": row.get("payout_display"), "wire_in_ready": bool(row.get("wire_in_ready")),
             "standing_gate": row.get("standing_gate")}
            for row in provider["offers"]
            if policy.normalized_relationship(row.get("relationship")) in policy.APPROVED_RELATIONSHIPS
        ]
    return selection


def expected_28_reconciliation(
    values: list[list[Any]], offers: list[dict[str, Any]], selection: dict[str, Any],
) -> dict[str, Any]:
    clinic = read_json(CLINIC_FITS, {})
    expected = [str(row.get("provider") or "") for row in clinic.get("providers") or []
                if not offer_exclusion_reason(row)]
    sheet_rows_by_provider: dict[str, list[int]] = {}
    sheet_offer_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    normalized_sheet_rows = {
        int(offer["sheet_row"]): offer
        for offer in sheet_offers(values)
        if offer.get("sheet_row")
    }
    for row_number, raw in enumerate(values[1:], 2):
        row = sheet_writer.pad(raw)
        provider = clean_provider_name(row[2])
        if provider:
            sheet_rows_by_provider.setdefault(slug(provider), []).append(row_number)
        network = policy.network_key(row[4])
        program_id = program_id_from_row(row)
        if provider and network and program_id:
            normalized = normalized_sheet_rows.get(row_number, {})
            sheet_offer_rows.setdefault((slug(provider), network, program_id), []).append({
                "row": row_number,
                "network": network,
                "program_id": program_id,
                "payout": normalized.get("headline_payout"),
                "payout_model": normalized.get("payout_model"),
                "payout_display": str(row[16] or ""),
                "approval_state": sheet_relationship(row[10]),
                "status": str(row[10] or ""),
                "wire_in_ready": bool(truthy(row[19]) or truthy(row[20])),
                "wired_live": truthy(row[20]),
            })
    offer_ids: dict[str, list[dict[str, str]]] = {}
    for offer in offers:
        offer_ids.setdefault(slug(offer["provider_name"]), []).append({
            "network": offer["network"], "program_id": str(offer.get("program_id") or ""),
            "payout": str(offer.get("payout_display") or offer.get("headline_payout") or ""),
            "relationship": str(offer.get("relationship") or ""),
            "wire_in_ready": str(bool(offer.get("wire_in_ready"))),
        })
    selected = matrix_index(selection)
    rows = []
    for provider in expected:
        key = slug(clean_provider_name(provider))
        selection_row = selected.get(key) or {}
        winner = selection_row.get("selected_winner") or {}
        selection_basis = str(selection_row.get("selection_basis") or "")
        wire_in_ready_offer_count = int(selection_row.get("wire_in_ready_offer_count") or 0)
        winner_key = (
            key,
            policy.network_key(winner.get("network")),
            str(winner.get("program_id") or ""),
        )
        winner_rows = sheet_offer_rows.get(winner_key, []) if winner_key[1] and winner_key[2] else []
        accuracy_issues = []
        if winner and not winner_rows:
            accuracy_issues.append("exact_winner_row_missing")
        elif winner:
            if not any(row["approval_state"] == "approved" for row in winner_rows):
                accuracy_issues.append("winner_approval_not_recorded")
            if not any(row["wire_in_ready"] for row in winner_rows):
                accuracy_issues.append("winner_wire_readiness_not_recorded")
            expected_payout = winner.get("payout")
            expected_model = str(winner.get("payout_model") or "").upper()
            if expected_payout is not None and not any(
                (
                    row["payout"] == expected_payout
                    or float(expected_payout) in [
                        float(value.replace(",", ""))
                        for value in re.findall(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", row["payout_display"])
                    ]
                )
                and policy.conversion_event({"payout_model": row["payout_model"]})
                == policy.conversion_event({"payout_model": expected_model})
                for row in winner_rows
            ):
                accuracy_issues.append("winner_payout_not_recorded_exactly")
        rows.append({
            "provider": clean_provider_name(provider),
            "sheet_rows": sheet_rows_by_provider.get(key, []),
            "offers": offer_ids.get(key, []),
            "sheet_recorded": bool(sheet_rows_by_provider.get(key)),
            "inventory_recorded": bool(offer_ids.get(key)),
            "selected_winner": {
                "network": winner_key[1],
                "program_id": winner_key[2],
                "payout": winner.get("payout"),
                "payout_model": winner.get("payout_model"),
            } if winner else None,
            "winner_sheet_rows": winner_rows,
            "winner_recorded_exactly": bool(winner_rows) and not accuracy_issues,
            "accuracy_issues": accuracy_issues,
            "selection_basis": selection_basis,
            "wire_in_ready_offer_count": wire_in_ready_offer_count,
            "winner_selection_blocked": not bool(winner),
        })
    duplicate_doc = read_json(PAYOUT_COMPARISONS, {})
    duplicate_names = [str(row.get("provider")) for row in duplicate_doc.get("duplicate_providers") or []
                       if row.get("scope") == "clinic_scout"]
    duplicate_keys = {slug(clean_provider_name(name)) for name in duplicate_names}
    duplicate = [row["provider"] for row in rows if slug(row["provider"]) in duplicate_keys]
    nonduplicate = [row["provider"] for row in rows if slug(row["provider"]) not in duplicate_keys]
    silent = [row["provider"] for row in rows if not row["sheet_recorded"] and not row["inventory_recorded"]]
    winner_gaps = [
        {"provider": row["provider"], "issues": row["accuracy_issues"],
         "selected_winner": row["selected_winner"]}
        for row in rows if row["accuracy_issues"]
    ]
    selection_blocks = [
        {"provider": row["provider"], "selection_basis": row["selection_basis"],
         "wire_in_ready_offer_count": row["wire_in_ready_offer_count"]}
        for row in rows if row["winner_selection_blocked"]
    ]
    return {
        "status": "PASS" if not silent and not winner_gaps and not selection_blocks else "PARTIAL",
        "expected_brand_count": len(expected),
        "rows": rows,
        "duplicate_brands": duplicate,
        "duplicate_brand_count": len(duplicate),
        "nonduplicate_brands": nonduplicate,
        "nonduplicate_brand_count": len(nonduplicate),
        "silent_drops": silent,
        "selected_winner_count": sum(bool(row["selected_winner"]) for row in rows),
        "exact_winner_rows_recorded": sum(row["winner_recorded_exactly"] for row in rows),
        "winner_recording_gaps": winner_gaps,
        "winner_selection_blocks": selection_blocks,
    }


def baseline_comparison_decisions(selection: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Reconcile the 18 source collisions against the current policy output."""
    document = read_json(PAYOUT_COMPARISONS, {})
    selected_by_provider = matrix_index(selection or {"providers": []})
    out = []
    for row in document.get("duplicate_providers") or []:
        if row.get("scope") != "clinic_scout":
            continue
        offers = [offer for offer in row.get("offers") or [] if isinstance(offer, dict)]
        if row.get("switch_staged"):
            candidates = [
                offer for offer in offers
                if policy.network_key(offer.get("network")) == "xentraffic"
            ]
            winner = max(
                candidates,
                key=lambda offer: (
                    float(offer.get("payout_per_lead") or 0),
                    -int(str(offer.get("program_id") or "0").split("/")[0]),
                ),
            ) if candidates else {}
        else:
            target_network = policy.network_key(row.get("target_network"))
            target_program_id = str(row.get("target_program_id") or "")
            winner = next((
                offer for offer in offers
                if policy.network_key(offer.get("network")) == target_network
                and str(offer.get("program_id") or "") == target_program_id
            ), {})
        source_winner_key = (
            policy.network_key(winner.get("network")),
            str(winner.get("program_id") or ""),
        )
        current = selected_by_provider.get(slug(clean_provider_name(row.get("provider"))))
        selected = (current or {}).get("selected_winner") or {}
        if current is not None or selection is not None:
            winner = selected
            switch_staged = bool(selected) and not bool(selected.get("wired_live"))
            selected_winner_key = (
                policy.network_key(selected.get("network")),
                str(selected.get("program_id") or ""),
            )
            if not selected:
                decision = "NO_WIRE_IN_READY_WINNER"
            elif (
                selected_winner_key == source_winner_key
                and switch_staged == bool(row.get("switch_staged"))
            ):
                decision = row.get("decision")
            elif switch_staged:
                decision = "STAGE_CURRENT_BEST_WIRE_IN_READY_WINNER"
            else:
                decision = "KEEP_CURRENT_BEST_WIRE_IN_READY_WINNER"
        else:
            switch_staged = bool(row.get("switch_staged"))
            decision = row.get("decision")
        out.append({
            "provider": clean_provider_name(row.get("provider")),
            "decision": decision,
            "switch_staged": switch_staged,
            "winner": {
                "network": policy.network_key(winner.get("network")),
                "program_id": str(winner.get("program_id") or ""),
                "payout": winner.get("payout", winner.get("payout_per_lead")),
                "payout_model": winner.get("payout_model"),
                "wire_in_ready": bool(winner.get("wire_in_ready")),
                "wired_live": bool(winner.get("wired_live")),
            },
            "target_sheet_row": winner.get("sheet_row") or row.get("target_sheet_row"),
            "source_queue_update_id": row.get("queue_update_id"),
        })
    return out


def winner_key(provider: dict[str, Any] | None) -> tuple[str, str]:
    winner = (provider or {}).get("selected_winner") or {}
    return policy.network_key(winner.get("network")), str(winner.get("program_id") or "")


def matrix_index(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {slug(row.get("provider_name")): row for row in matrix.get("providers") or []}


def winner_ranking_value(winner: dict[str, Any], *, cross_event: bool) -> Any:
    if cross_event:
        return winner.get("expected_earnings_per_100_clicks")
    return winner.get("effective_value", winner.get("payout"))


def changed_winners(previous: dict[str, Any], current: dict[str, Any], scope: set[str] | None = None) -> list[dict[str, Any]]:
    before, after = matrix_index(previous), matrix_index(current)
    changes = []
    for key, row in after.items():
        if scope is not None and key not in scope:
            continue
        old_key, new_key = winner_key(before.get(key)), winner_key(row)
        if old_key == new_key or new_key == ("", ""):
            continue
        if old_key == ("", "") and bool((row.get("selected_winner") or {}).get("wired_live")):
            # A newly observed matrix row that is already the live route is a
            # baseline fact, not a deploy backlog switch.
            continue
        old_winner = (before.get(key) or {}).get("selected_winner") or {}
        new_winner = row.get("selected_winner") or {}
        # This is a live tracking-link SWAP queue. Initial wire-ins already flow
        # from the Sheet into B2. Never stage a swap unless the prior winner is
        # proved live and the new same-event payout or cross-event expected
        # earnings per 100 clicks clears the material-uplift floor.
        if old_key != ("", ""):
            if not bool(old_winner.get("wired_live")):
                continue
            same_currency = (
                str(old_winner.get("currency") or "USD").upper()
                == str(new_winner.get("currency") or "USD").upper()
            )
            cross_event = (
                policy.comparison_event(old_winner) != policy.comparison_event(new_winner)
            )
            old_value = winner_ranking_value(old_winner, cross_event=cross_event)
            new_value = winner_ranking_value(new_winner, cross_event=cross_event)
            if not same_currency or old_value in (None, "") or new_value in (None, ""):
                continue
            try:
                materially_better = (
                    policy.decimal_value(new_value)
                    >= policy.decimal_value(old_value)
                    * policy.DEFAULT_SWITCH_RATIO
                )
            except ValueError:
                materially_better = False
            if not materially_better:
                continue
        changes.append({
            "provider": row.get("provider_name"),
            "prior_winner": {"network": old_key[0], "program_id": old_key[1]} if old_key != ("", "") else None,
            "winner": row.get("selected_winner"),
            "decision": "STAGE_BEST_COLLECTIBLE_WINNER_CHANGE",
            "switch_staged": True,
            "target_sheet_row": (row.get("selected_winner") or {}).get("sheet_row"),
        })
    return changes


def stage_backlog(
    switches: list[dict[str, Any]], run_path: Path,
    current_matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_path = SWITCH_BACKLOG.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        backlog = read_json(SWITCH_BACKLOG, {
            "schema": "glp.best-terms-switch-backlog.v1",
            "items": [],
            "effects": {"live_link_changes": 0, "deploys": 0},
        })
        desired_ids = {}
        for switch in switches:
            winner = switch.get("winner") or {}
            desired_ids[slug(switch.get("provider"))] = (
                "best-terms-" + slug(switch.get("provider")) + "-"
                + slug(winner.get("network")) + "-" + slug(winner.get("program_id"))
            )
        current_index = matrix_index(current_matrix or {"providers": []})
        removed_redundant_live = []
        removed_superseded = []
        removed_invalid_current = []
        kept = []
        for item in backlog.get("items") or []:
            provider_key = slug(item.get("provider"))
            item_id = str(item.get("switch_id") or "")
            current = current_index.get(provider_key) or {}
            current_winner = current.get("selected_winner") or {}
            item_winner = item.get("winner") or {}
            item_key = (
                policy.network_key(item_winner.get("network")),
                str(item_winner.get("program_id") or ""),
            )
            current_key = (
                policy.network_key(current_winner.get("network")),
                str(current_winner.get("program_id") or ""),
            )
            already_live = bool(item_winner.get("wired_live")) or (
                item_key == current_key and bool(current_winner.get("wired_live"))
            )
            if already_live:
                removed_redundant_live.append(item)
                continue
            if current and current_key == ("", ""):
                removed_invalid_current.append(item)
                continue
            if current and item_key != current_key:
                removed_superseded.append(item)
                continue
            if provider_key in desired_ids and item_id != desired_ids[provider_key]:
                removed_superseded.append(item)
                continue
            if current and item_key == current_key:
                item = {**item, "winner": current_winner}
            kept.append(item)
        backlog["items"] = kept
        existing = {str(item.get("switch_id")): item for item in backlog.get("items") or []}
        added = []
        for switch in switches:
            winner = switch.get("winner") or {}
            switch_id = "best-terms-" + slug(switch.get("provider")) + "-" + slug(winner.get("network")) + "-" + slug(winner.get("program_id"))
            if switch_id in existing:
                continue
            item = {
                "switch_id": switch_id,
                "provider": switch.get("provider"),
                "prior_winner": switch.get("prior_winner"),
                "winner": winner,
                "decision": switch.get("decision"),
                "status": "STAGED_NOT_DEPLOYED",
                "consumer": "scripts/glp_affiliate_wire_backlog_worker.py",
                "queue_kind": "affiliate_tracking_link_swap",
                "staged_at_utc": utc_now(),
                "decision_run": repo_rel(run_path),
                "target_sheet_row": switch.get("target_sheet_row"),
                "effects_now": {"live_link_changes": 0, "deploys": 0, "repoints": 0},
            }
            backlog.setdefault("items", []).append(item)
            existing[switch_id] = item
            added.append(item)
        backlog["updated_at_utc"] = utc_now()
        atomic_json(SWITCH_BACKLOG, backlog)
        fcntl.flock(handle, fcntl.LOCK_UN)
    return {"added": added, "added_count": len(added), "total": len(existing),
            "removed_redundant_live": removed_redundant_live,
            "removed_superseded": removed_superseded,
            "removed_invalid_current": removed_invalid_current,
            "path": repo_rel(SWITCH_BACKLOG), "sha256": sha256_file(SWITCH_BACKLOG)}


def sheet_row_for_switch(values: list[list[Any]], switch: dict[str, Any]) -> tuple[int, list[Any]] | None:
    requested = switch.get("target_sheet_row")
    if isinstance(requested, int) and 1 < requested <= len(values):
        return requested, sheet_writer.pad(values[requested - 1])
    provider_key = slug(switch.get("provider"))
    matches = []
    for number, raw in enumerate(values[1:], 2):
        row = sheet_writer.pad(raw)
        if slug(clean_provider_name(row[2])) == provider_key:
            matches.append((number, row))
    return matches[0] if matches else None


def payout_queue_rows(values: list[list[Any]], switches: list[dict[str, Any]], evidence_path: Path) -> list[dict[str, Any]]:
    digest = sha256_file(evidence_path)
    rows = []
    for switch in switches:
        target = sheet_row_for_switch(values, switch)
        if not target:
            continue
        row_number, row = target
        winner = switch.get("winner") or {}
        incumbent = parse_payout(row[16])
        evidence = {
            "payout_evaluation_path": repo_rel(evidence_path),
            "payout_evaluation_sha256": digest,
            "evaluated_at_utc": utc_now(),
            "provider_identity": f"name:{slug(switch.get('provider'))}",
            "basis": "headline",
            "formula": "same event: highest payout; different events: measured EPC or configured CPA/CPL priors expressed as earnings per 100 clicks",
            "incumbent_effective_value": str(incumbent.get("headline_payout") or row[16] or "not recorded"),
            "challenger_effective_value": str(winner.get("effective_value") or winner.get("payout") or "recorded in matrix"),
            "decision": str(switch.get("decision") or "STAGE_BEST_COLLECTIBLE_WINNER_CHANGE"),
            "winner_network": winner.get("network"),
            "winner_program_id": str(winner.get("program_id") or ""),
            "live_link_changes": 0,
            "deploys": 0,
        }
        update_id = "best-terms-" + slug(switch.get("provider")) + "-" + slug(winner.get("network")) + "-" + slug(winner.get("program_id"))
        rows.append({
            "update_id": update_id,
            "kind": "payout_evaluation",
            "network": str(row[4] or ""),
            "program_id": program_id_from_row(row),
            "program_name": str(row[2] or switch.get("provider") or ""),
            # n8n's sheet_update_queue schema declares sheet_row as a number.
            # Native REST coerces numeric strings on insert, and exact write
            # reconciliation correctly treats "759" != 759 as a mismatch.
            "sheet_row": row_number,
            "evidence": json.dumps(evidence, sort_keys=True),
            "status": "queued",
            "queued_by": "affiliate_best_terms_machine",
            "queued_at_utc": utc_now(),
            "resolved_at_utc": "",
            "note": "Best collectible offer decision staged for the big deploy run. No live link, re-point, or deployment executed.",
        })
    return rows


def route_parts(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if ":" not in text:
        raise ValueError(f"payout_leak_route_invalid:{text!r}")
    network, program_id = text.split(":", 1)
    network = policy.network_key(network)
    program_id = program_id.strip()
    if not network or not re.fullmatch(r"[A-Za-z0-9._/-]+", program_id):
        raise ValueError(f"payout_leak_route_invalid:{text!r}")
    return network, program_id


def live_winner_sheet_row(
    values: list[list[Any]], *, winner_network: str, winner_program_id: str,
) -> int | None:
    """Resolve a newly materialized winner row without rewriting sweep evidence.

    The payout sweep is an immutable economic decision snapshot.  A winner row
    can legitimately be created later through the governed ``new_approval``
    writer, so the original offer matrix still says ``sheet_row: null``.  The
    queue projection must reconcile that one missing pointer against the current
    Sheet while keeping its source hash bound to the original decision.  Exact
    network + provider-program identity is required; ambiguity fails closed.
    """
    network = policy.network_key(winner_network)
    program_id = str(winner_program_id or "").strip()
    matches: list[int] = []
    for row_number, raw in enumerate(values[1:], 2):
        row = sheet_writer.pad(raw)
        if policy.network_key(row[4]) != network:
            continue
        if program_id_from_row(row) == program_id:
            matches.append(row_number)
    if len(matches) > 1:
        raise ValueError(
            f"payout_leak_winner_sheet_row_ambiguous:{network}:{program_id}:{matches}"
        )
    return matches[0] if matches else None


def _payout_leak_fixture(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(path).resolve()
    document = read_json(path, None)
    if not isinstance(document, dict) or document.get("schema") != "glp.payout-leak-quantified.v1":
        raise ValueError("payout_leak_fixture_schema_invalid")
    leaks = document.get("leaks")
    if not isinstance(leaks, list) or not leaks or not all(isinstance(row, dict) for row in leaks):
        raise ValueError("payout_leak_fixture_rows_invalid")
    declared = int((((document.get("summary") or {}).get("leaking_row_count") or {}).get("value") or 0))
    if declared != len(leaks):
        raise ValueError(f"payout_leak_fixture_denominator_mismatch:{declared}:{len(leaks)}")
    source = document.get("source_matrix") or {}
    matrix_path = Path(str(source.get("path") or "")).resolve()
    if not matrix_path.is_file() or sha256_file(matrix_path) != str(source.get("sha256") or ""):
        raise ValueError("payout_leak_source_matrix_binding_invalid")
    matrix = read_json(matrix_path, None)
    if not isinstance(matrix, dict) or matrix.get("schema") != "glp.payout-leak-offer-matrix.v1":
        raise ValueError("payout_leak_source_matrix_schema_invalid")
    return document, matrix


def payout_leak_queue_rows(
    values: list[list[Any]], leak_path: Path = PAYOUT_LEAK_SOURCE,
) -> list[dict[str, Any]]:
    """Project every quantified leak row into the proven payout_evaluation lane.

    The queue remains row-granular because the governed writer records one fact
    on one Sheet row.  The deploy instruction is deliberately provider-granular:
    every row for the same provider carries the same complete affected-row set,
    so no consumer can mistake one surfaced leak for the deployment boundary.
    """
    leak_path = Path(leak_path).resolve()
    document, matrix = _payout_leak_fixture(leak_path)
    digest = sha256_file(leak_path)
    leaks = list(document["leaks"])
    provider_rows: dict[str, list[dict[str, Any]]] = {}
    for leak in leaks:
        provider_rows.setdefault(str(leak.get("provider_identity") or ""), []).append(leak)

    offers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for offer in matrix.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        route = f"{policy.network_key(offer.get('network'))}:{str(offer.get('program_id') or '')}"
        offers.setdefault((str(offer.get("provider_identity") or ""), route), []).append(offer)

    generated = str(document.get("generated_at_utc") or utc_now())
    rows: list[dict[str, Any]] = []
    for leak in leaks:
        provider = str(leak.get("provider") or "").strip()
        provider_identity = str(leak.get("provider_identity") or "").strip()
        current_route = str(leak.get("current_route") or "").strip().casefold()
        winner_route = str(leak.get("best_available_route") or "").strip().casefold()
        current_network, current_program_id = route_parts(current_route)
        winner_network, winner_program_id = route_parts(winner_route)
        row_number = int(leak.get("current_sheet_row") or 0)
        if row_number < 2 or row_number > len(values):
            raise ValueError(f"payout_leak_sheet_row_out_of_bounds:{row_number}")
        sheet_row = sheet_writer.pad(values[row_number - 1])
        if policy.network_key(sheet_row[4]) != current_network:
            raise ValueError(f"payout_leak_sheet_network_mismatch:{row_number}")
        observed_program_id = program_id_from_row(sheet_row)
        if observed_program_id and observed_program_id != current_program_id:
            raise ValueError(f"payout_leak_sheet_program_id_mismatch:{row_number}")

        siblings = provider_rows.get(provider_identity) or []
        affected_rows = sorted({int(item.get("current_sheet_row") or 0) for item in siblings})
        affected_routes = sorted({str(item.get("current_route") or "").casefold() for item in siblings})
        provider_winners = {str(item.get("best_available_route") or "").casefold() for item in siblings}
        if provider_winners != {winner_route}:
            raise ValueError(f"payout_leak_provider_winner_conflict:{provider_identity}")

        winner_offers = offers.get((provider_identity, winner_route)) or []
        if len(winner_offers) != 1:
            raise ValueError(
                f"payout_leak_winner_offer_match_count:{provider_identity}:{winner_route}:"
                f"{len(winner_offers)}"
            )
        winner_offer = winner_offers[0]
        raw_winner_row = winner_offer.get("sheet_row")
        winner_sheet_row = int(raw_winner_row) if str(raw_winner_row or "").isdigit() else None
        if winner_sheet_row is None:
            winner_sheet_row = live_winner_sheet_row(
                values,
                winner_network=winner_network,
                winner_program_id=winner_program_id,
            )
        instruction = (
            f"Preserve every existing {provider} provider-issued link until one validated atomic "
            f"deploy. During that deploy, switch ALL {provider} tracking links and monetized "
            f"surfaces for Sheet rows {affected_rows} from lower-payout routes "
            f"{affected_routes} to the held higher-payout rail {winner_route}; verify every "
            "provider surface by href/config inspection only, then enqueue Sheet updates through "
            "the single writer so the winning rail is the sole Wired Live route and every losing "
            "rail remains approved/tracking-ready but not live. Never request or click a tracking link."
        )
        evidence = {
            "payout_evaluation_path": repo_rel(leak_path),
            "payout_evaluation_sha256": digest,
            "evaluated_at_utc": generated,
            "provider": provider,
            "provider_identity": provider_identity,
            "basis": "headline",
            "formula": "same paid event: highest authenticated payout on a held rail wins",
            "incumbent_effective_value": str(((leak.get("current_payout") or {}).get("value"))),
            "challenger_effective_value": str(((leak.get("best_available_payout") or {}).get("value"))),
            "decision": PAYOUT_LEAK_DECISION,
            "deploy_instruction_version": PAYOUT_LEAK_INSTRUCTION_VERSION,
            "deploy_instruction": instruction,
            "provider_all_links": True,
            "preserve_links_until_atomic_deploy": True,
            "sheet_updates_via_single_writer_only": True,
            "href_config_inspection_only": True,
            "tracking_clicks": 0,
            "current_route": current_route,
            "current_network": current_network,
            "current_program_id": current_program_id,
            "current_sheet_row": row_number,
            "current_state": str(leak.get("current_state") or ""),
            "winner_route": winner_route,
            "winner_network": winner_network,
            "winner_program_id": winner_program_id,
            "winner_sheet_row": winner_sheet_row,
            "winner_state": str(winner_offer.get("current_state") or ""),
            "affected_sheet_rows": affected_rows,
            "affected_routes": affected_routes,
            "provider_leak_row_count": len(siblings),
            "delta_usd_per_conversion": (leak.get("delta") or {}).get("value"),
            "guardrail": "stage only; zero deploys and zero live changes in this queueing job",
        }
        update_id = (
            f"payout-leak-{row_number}-{slug(current_route)}-to-{slug(winner_route)}-{digest[:12]}"
        )
        rows.append({
            "update_id": update_id,
            "kind": "payout_evaluation",
            "network": str(sheet_row[4] or ""),
            "program_id": current_program_id,
            "program_name": str(sheet_row[2] or provider),
            "sheet_row": row_number,
            "evidence": json.dumps(evidence, sort_keys=True),
            "status": "queued",
            "queued_by": "affiliate_best_terms_machine:payout_leak_sweep_20260830",
            "queued_at_utc": utc_now(),
            "resolved_at_utc": "",
            "note": (
                f"Provider-wide higher-payout switch staged for deploy; all {len(affected_rows)} "
                "provider route rows are covered. No link opened/clicked and nothing deployed."
            ),
        })
    return rows


def _queue_evidence(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("evidence")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _payout_leak_semantic_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    evidence = _queue_evidence(row)
    provider = str(evidence.get("provider_identity") or "").casefold()
    current = str(evidence.get("current_route") or "").casefold()
    winner = str(evidence.get("winner_route") or "").casefold()
    if provider and current and winner:
        return provider, current, winner
    # The five-day-old ShedRX fact predates the general fields.  Its immutable
    # update id and exact decision are the proven semantic identity; enriching
    # that same row is an idempotent upgrade, never a duplicate instruction.
    if (
        str(row.get("update_id") or "")
        == "payout-matrix-shedrx-awin-80963-katalys-1516-30fa835fcd45"
        and evidence.get("decision") == "STAGE_DEDUP_SWITCH_AWIN_TO_KATALYS_AT_DEPLOY"
    ):
        return "domain:shedrx.com", "awin:80963", "katalys:1516"
    return None


def enqueue_payout_leaks(
    leak_path: Path = PAYOUT_LEAK_SOURCE, *, n8n_client: Any = None,
) -> dict[str, Any]:
    """Idempotently enqueue and drain the quantified leaks through one writer."""
    import n8n_dt as default_n8n  # noqa: PLC0415

    client = n8n_client or default_n8n
    leak_path = Path(leak_path).resolve()
    values = read_live_sheet()
    planned = payout_leak_queue_rows(values, leak_path)
    by_semantic: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for planned_row in planned:
        key = _payout_leak_semantic_key(planned_row)
        candidate_ids = [str(planned_row["update_id"])]
        if key == ("domain:shedrx.com", "awin:80963", "katalys:1516"):
            candidate_ids.append(
                "payout-matrix-shedrx-awin-80963-katalys-1516-30fa835fcd45"
            )
        matches: dict[str, dict[str, Any]] = {}
        for update_id in candidate_ids:
            for existing_row in client.rows(
                SHEET_QUEUE_TABLE, client.eq("update_id", update_id)
            ):
                if _payout_leak_semantic_key(existing_row) == key:
                    matches[str(existing_row.get("update_id") or "")] = existing_row
        if matches:
            by_semantic[key] = list(matches.values())

    inserted_rows: list[dict[str, Any]] = []
    already_present: list[str] = []
    upgraded: list[str] = []
    for row in planned:
        key = _payout_leak_semantic_key(row)
        if key is None:
            raise RuntimeError("payout_leak_planned_semantic_key_missing")
        matches = by_semantic.get(key) or []
        if len(matches) > 1:
            raise RuntimeError(f"payout_leak_semantic_duplicate:{key}:{len(matches)}")
        if matches:
            existing_row = matches[0]
            already_present.append(str(existing_row.get("update_id") or ""))
            existing_evidence = _queue_evidence(existing_row)
            desired_evidence = _queue_evidence(row)
            missing = {
                name: value for name, value in desired_evidence.items()
                if existing_evidence.get(name) != value
                and name in {
                    "provider", "provider_identity", "deploy_instruction_version",
                    "deploy_instruction", "provider_all_links",
                    "preserve_links_until_atomic_deploy", "sheet_updates_via_single_writer_only",
                    "href_config_inspection_only", "tracking_clicks", "current_route",
                    "current_network", "current_program_id", "current_sheet_row", "current_state",
                    "winner_route", "winner_network", "winner_program_id", "winner_sheet_row",
                    "winner_state", "affected_sheet_rows", "affected_routes",
                    "provider_leak_row_count", "delta_usd_per_conversion", "guardrail",
                }
            }
            if missing:
                merged = {**existing_evidence, **missing}
                client.update(
                    SHEET_QUEUE_TABLE,
                    client.eq("update_id", str(existing_row.get("update_id") or "")),
                    {"evidence": json.dumps(merged, sort_keys=True)},
                )
                upgraded.append(str(existing_row.get("update_id") or ""))
            continue
        client.insert(SHEET_QUEUE_TABLE, row)
        landed = client.rows(SHEET_QUEUE_TABLE, client.eq("update_id", row["update_id"]))
        if len(landed) != 1 or _payout_leak_semantic_key(landed[0]) != key:
            raise RuntimeError(f"payout_leak_queue_readback_failed:{row['update_id']}")
        inserted_rows.append(row)
        by_semantic[key] = landed

    run_dir = leak_path.parent / f"queue-drain-{stamp()}"
    drain = drain_queue(inserted_rows, run_dir)
    inserted_ids = {row["update_id"] for row in inserted_rows}
    result_by_id = {
        str(row.get("update_id") or ""): row for row in (drain.get("results") or [])
    }
    blocked = sorted(
        update_id for update_id in inserted_ids
        if str((result_by_id.get(update_id) or {}).get("status") or "")
        not in {"applied", "noop"}
    )
    planned_keys = {_payout_leak_semantic_key(row) for row in planned}
    final_keys = set()
    for planned_row in planned:
        key = _payout_leak_semantic_key(planned_row)
        candidate_ids = [str(planned_row["update_id"])]
        if key == ("domain:shedrx.com", "awin:80963", "katalys:1516"):
            candidate_ids.append(
                "payout-matrix-shedrx-awin-80963-katalys-1516-30fa835fcd45"
            )
        for update_id in candidate_ids:
            matches = client.rows(SHEET_QUEUE_TABLE, client.eq("update_id", update_id))
            if len(matches) == 1 and _payout_leak_semantic_key(matches[0]) == key:
                final_keys.add(key)
                break
    missing = sorted(str(key) for key in planned_keys - final_keys)
    if missing:
        raise RuntimeError(f"payout_leak_queue_final_readback_missing:{missing}")

    receipt = {
        "schema": "glp.payout-leak-deploy-queue-receipt.v1",
        "status": "PASS" if not blocked else "BLOCKED",
        "captured_at_utc": utc_now(),
        "source": {"path": str(leak_path), "sha256": sha256_file(leak_path)},
        "queue_table": SHEET_QUEUE_TABLE,
        "queue_kind": "payout_evaluation",
        "plan_schema": "affiliate-payout-evaluation-plan-v1",
        "rows_expected": len(planned),
        "providers_expected": len({_queue_evidence(row)["provider_identity"] for row in planned}),
        "queued_new": len(inserted_rows),
        "already_present": len(already_present),
        "already_present_update_ids": already_present,
        "enriched_in_place": len(upgraded),
        "enriched_update_ids": upgraded,
        "blocked": len(blocked),
        "blocked_update_ids": blocked,
        "semantic_keys_present": len(planned_keys),
        "drain": drain,
        "effects": {
            "deployments": 0,
            "live_tracking_changes": 0,
            "provider_mutations": 0,
            "applications": 0,
            "tracking_links_opened_or_clicked": 0,
            "tracking_verification": "href/config inspection only",
            "sheet_write_path": "sheet_update_queue -> glp_sheet_sync_drain.py -> affiliate_sheet_batch_writer.py",
        },
    }
    atomic_json(PAYOUT_LEAK_QUEUE_RECEIPT, receipt)
    return receipt


def drain_queue(rows: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    if not rows:
        return {"status": "NOOP", "queued": 0, "results": []}
    import n8n_dt  # noqa: PLC0415

    existing = n8n_dt.native_rows_for_keys(
        SHEET_QUEUE_TABLE, "update_id", {row["update_id"] for row in rows})
    pending = [row for row in rows if row["update_id"] not in existing]
    batch = n8n_dt.insert_many(SHEET_QUEUE_TABLE, pending)
    inserted = int(batch.get("inserted") or 0)
    completed = None
    drain_output = run_dir / "sheet-drain" / "run-summary.json"
    drain_output.parent.mkdir(parents=True, exist_ok=True)
    # The whole batch goes to the drain in a file. Serialised into one argv
    # element it crosses Linux's 128 KB MAX_ARG_STRLEN at a few dozen rows and
    # the exec dies E2BIG before the drain starts. See scripts/sheet_drain_invoke.py.
    with sheet_drain_invoke.queue_file(rows, prefix="best-terms-queue-") as queue_path:
        for attempt in range(len(SHEET_LOCK_RETRY_SECONDS) + 1):
            completed = subprocess.run(
                [sys.executable, str(SHEET_DRAIN), *sheet_drain_invoke.drain_args(queue_path),
                 "--apply", "true", "--output", str(drain_output)],
                cwd=BUSINESS, capture_output=True, text=True, timeout=900,
            )
            if completed.returncode != SHEET_LOCK_RC:
                break
            if attempt < len(SHEET_LOCK_RETRY_SECONDS):
                time.sleep(SHEET_LOCK_RETRY_SECONDS[attempt])
    assert completed is not None
    if completed.returncode == SHEET_LOCK_RC:
        return {"status": "DEFERRED_LOCK_BUSY", "rc": SHEET_LOCK_RC, "inserted": inserted,
                "queued": len(rows), "results": []}
    if completed.returncode != 0:
        return {"status": "FAILED", "rc": completed.returncode, "inserted": inserted,
                "queued": len(rows), "error": completed.stderr[-500:]}
    try:
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "FAILED", "rc": completed.returncode, "inserted": inserted,
                "queued": len(rows), "error": "sheet_drain_output_invalid"}
    for result in summary.get("results") or []:
        status = str(result.get("status") or "")
        if status in {"applied", "noop", "refused", "escalated", "deferred"}:
            n8n_dt.update(SHEET_QUEUE_TABLE, n8n_dt.eq("update_id", str(result.get("update_id") or "")), {
                "status": status,
                "resolved_at_utc": utc_now(),
                "note": str(result.get("reason") or "")[:900],
            })
    return {"status": "PASS", "rc": 0, "inserted": inserted, "queued": len(rows),
            "results": summary.get("results") or [], "receipt": summary.get("receipt")}


def approval_receipts_since_cursor(cursor: dict[str, Any], root: Path = APPROVAL_REPORT_ROOT,
                                   *, values: list[list[Any]] | None = None) -> list[dict[str, Any]]:
    seen = set(cursor.get("processed_receipts") or [])
    out = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        rel = str(path)
        if rel in seen:
            continue
        payload = read_json(path, {})
        if (payload.get("status") == "PASS" and payload.get("approval_processing") is True
                and policy.normalized_relationship(payload.get("provider_relationship")) in policy.APPROVED_RELATIONSHIPS
                and payload.get("row_number")):
            out.append({"path": rel, "payload": payload})
    # This tenant enters through the researched-row queue, not the legacy
    # approval reconciler. Observe the actual governed Sheet transition.
    for number, raw in enumerate((values or [])[1:], 2):
        row = sheet_writer.pad(raw)
        network = policy.network_key(row[4])
        if network != "roeverflowwhitelabeltenant4131" or sheet_relationship(row[10]) != "approved":
            continue
        program_id = program_id_from_row(row)
        if not program_id:
            continue
        fingerprint = hashlib.sha256(json.dumps(
            ["current-xentraffic-availability-v1", number,
             *[row[i] for i in (2, 4, 5, 10, 16)]], sort_keys=True,
        ).encode()).hexdigest()
        key = f"sheet-approval:{network}:{program_id}:{fingerprint}"
        if key not in seen:
            out.append({"path": key, "payload": {
                "source": "governed_sheet_ro_tenant_approval", "row_number": number,
                "comparison_revision": "current-xentraffic-availability-v1",
                "network": network, "program_id": program_id,
            }})
    return out


def approval_affected_providers(receipts: list[dict[str, Any]], values: list[list[Any]]) -> set[str]:
    providers = set()
    for item in receipts:
        number = int((item.get("payload") or {}).get("row_number") or 0)
        if 1 < number <= len(values):
            providers.add(slug(clean_provider_name(sheet_writer.pad(values[number - 1])[2])))
    return providers


def discord_message(result: dict[str, Any]) -> str:
    census_partial = result.get("census", {}).get("status") == "PARTIAL"
    sheet_gaps = len(result.get("reconciliation", {}).get("winner_recording_gaps") or [])
    selection_blocks = len(result.get("reconciliation", {}).get("winner_selection_blocks") or [])
    partial = census_partial or bool(sheet_gaps) or bool(selection_blocks)
    changed = int(result.get("switch_backlog", {}).get("added_count") or 0)
    failures = len(result.get("census", {}).get("failures") or [])
    if not partial and not changed:
        return ""
    status = "warn" if partial else "ok"
    if partial:
        parts = []
        if census_partial:
            parts.append(f"census PARTIAL · {failures} marketplace failure{'s' if failures != 1 else ''}")
        if sheet_gaps:
            parts.append(f"{sheet_gaps} Sheet winner gap{'s' if sheet_gaps != 1 else ''}")
        if selection_blocks:
            parts.append(f"{selection_blocks} winner selection block{'s' if selection_blocks != 1 else ''}")
        headline = " · ".join(parts)
    else:
        headline = f"{changed} winner change{'s' if changed != 1 else ''} staged"
    message = render(
        title(status, "Best Terms", headline),
        f"winners changed: **{changed}** · switches staged: **{changed}**",
        meaning("Review named census failures or paid-event selection blocks; no hand-off was dropped and no links changed, re-pointed, or deployed." if partial
                else "The big deploy backlog has the new winners; no live link changed."),
        subtext(f"receipt: {repo_rel(LATEST_RUN)}"),
        output_class="alert" if partial else "report",
    )
    expected_class = "alert" if partial else "report"
    checked = lint_discord(message, expected_class)
    if checked.get("status") != "pass":
        raise RuntimeError(f"discord_fixture_lint_failed:{checked.get('errors')}")
    return message


def send_discord(content: str) -> dict[str, Any]:
    if not content:
        return {"status": "SILENT"}
    from inbox_control_plane.control_plane import discord_request  # noqa: PLC0415

    created = discord_request(
        f"/channels/{OPS_ALERTS_CHANNEL}/messages", "POST",
        {"content": content, "allowed_mentions": {"parse": []}},
    )
    message_id = str(created.get("id") or "") if isinstance(created, dict) else ""
    if not message_id:
        raise RuntimeError("discord_message_id_missing")
    readback = discord_request(f"/channels/{OPS_ALERTS_CHANNEL}/messages/{message_id}")
    if not isinstance(readback, dict) or readback.get("content") != content:
        raise RuntimeError("discord_readback_mismatch")
    return {"status": "PASS", "channel_id": OPS_ALERTS_CHANNEL,
            "message_id": message_id, "readback_exact": True}


def failure_signature(exc: Exception) -> str:
    """Stable, bounded identity; never persist raw provider stderr as a key."""
    text = str(exc)
    if "xentraffic_stale_or_foreign_singleton_lock" in text:
        return "xentraffic_stale_or_foreign_singleton_lock"
    return f"{type(exc).__name__}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def approval_lock_held() -> bool:
    state = read_json(FAILURE_STATE, {})
    if state.get("status") != "ACTIVE" or state.get("signature") != "xentraffic_stale_or_foreign_singleton_lock":
        return False
    try:
        last = dt.datetime.fromisoformat(state["last_alert_utc"].replace("Z", "+00:00"))
        return dt.datetime.now(dt.timezone.utc) - last < dt.timedelta(hours=24)
    except (KeyError, ValueError, TypeError):
        # Unknown prior disposition must not silently suppress attempts forever.
        return False


def record_failure(exc: Exception, *, send_alert: bool) -> dict[str, Any]:
    signature = failure_signature(exc)
    state = read_json(FAILURE_STATE, {})
    now = utc_now()
    try:
        last = dt.datetime.fromisoformat(state["last_alert_utc"].replace("Z", "+00:00"))
        fresh = dt.datetime.now(dt.timezone.utc) - last < dt.timedelta(hours=24)
    except (KeyError, ValueError, TypeError):
        fresh = False
    if state.get("status") == "ACTIVE" and state.get("signature") == signature and fresh:
        return state.get("notification") or {"status": "SUPPRESSED_SAME_SIGNATURE"}
    # Reserve before POST: an unknown acknowledgement must never be blindly resent.
    state = {"status": "ACTIVE", "signature": signature,
             "first_seen_utc": state.get("first_seen_utc", now) if state.get("signature") == signature else now,
             "last_alert_utc": now, "notification": {"status": "PENDING_UNRECONCILED"}}
    atomic_json(FAILURE_STATE, state)
    if send_alert:
        message = render(title("action", "Best Terms", "machine failed before a trustworthy matrix"),
                         meaning("Inspect the named local failure; pending approvals remain pending and no live link changed."),
                         subtext(f"signature: {signature} · receipt: {repo_rel(LATEST_RUN)}"), output_class="alert")
        notification = send_discord(message)
    else:
        notification = {"status": "NOT_REQUESTED"}
    state["notification"] = notification
    atomic_json(FAILURE_STATE, state)
    return notification


def record_failure_recovery(*, send_alert: bool) -> dict[str, Any]:
    state = read_json(FAILURE_STATE, {})
    if state.get("status") != "ACTIVE":
        return {"status": "SILENT"}
    state["status"] = "RECOVERED"
    state["recovered_at_utc"] = utc_now()
    atomic_json(FAILURE_STATE, state)
    if not send_alert:
        return {"status": "NOT_REQUESTED"}
    message = render(title("ok", "Best Terms", "approval evaluation recovered"),
                     meaning("The previous failure signature cleared; use the current run receipt for actual winner and queue effects."),
                     subtext(f"prior signature: {state['signature']} · receipt: {repo_rel(LATEST_RUN)}"), output_class="report")
    return send_discord(message)


def run_machine(
    *, first_run: bool, approval_events: bool, stage: bool,
    census_dir: Path | None = None, xentraffic_catalog: Path | None = None,
    send_alert: bool = False,
) -> dict[str, Any]:
    weekly_guard = None
    if not approval_events:
        weekly_guard = claim_weekly_window()
        if weekly_guard["duplicate"]:
            return {
                "schema": "glp.best-terms-run.v1",
                "status": "PASS_DUPLICATE_WINDOW",
                "mode": "weekly",
                "generated_at_utc": utc_now(),
                "weekly_window": weekly_guard,
                "effects": {"deploys": 0, "live_link_changes": 0, "repoints": 0},
            }
    run_dir = RUNS_ROOT / f"run-{stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    previous = read_json(LATEST_MATRIX, {"providers": []})
    values = read_live_sheet()

    trigger_receipts: list[dict[str, Any]] = []
    trigger_scope: set[str] | None = None
    cursor = read_json(APPROVAL_CURSOR, {"processed_receipts": []})
    if approval_events:
        trigger_receipts = approval_receipts_since_cursor(cursor, values=values)
        trigger_scope = approval_affected_providers(trigger_receipts, values)
        if not trigger_receipts:
            result = {"schema": "glp.best-terms-run.v1", "status": "PASS_NO_NEW_APPROVALS",
                      "generated_at_utc": utc_now(), "effects": {"deploys": 0, "live_link_changes": 0}}
            atomic_json(run_dir / "run-receipt.json", result)
            return result

        if "xentraffic" not in HELD_NETWORKS and xentraffic_catalog is None and approval_lock_held():
            # No cursor advance, no stale-catalog substitution, no census/browser
            # retry on every inbox poll. A daily retry or a new safe collector
            # deployment must establish recovery through the registered path.
            raise RuntimeError("xentraffic_stale_or_foreign_singleton_lock")

    if census_dir is None and not approval_events:
        census_dir, supply_summary = run_supply_census(run_dir)
    elif census_dir is not None:
        supply_summary = read_json(census_dir / "_sweep-summary.json", {"networks": {}, "gaps": []})
    else:
        supply_summary = {"networks": (read_json(LATEST_RUN, {}).get("census") or {}).get("networks", {}), "gaps": []}

    if "xentraffic" in HELD_NETWORKS:
        xentraffic_catalog = None
        xen_summary = held_source_summary("xentraffic")
    elif xentraffic_catalog is None:
        xentraffic_catalog, xen_summary = run_xentraffic_census(run_dir)
    else:
        xentraffic_catalog = latest_xentraffic_catalog(xentraffic_catalog)
        xen_summary = supplied_xentraffic_summary(xentraffic_catalog)
    census = census_contract(supply_summary, xen_summary)
    if approval_events:
        census["status"] = "PROVIDER_SCOPED_TRIGGER"
        census["denominator_proven"] = None
        census["failures"] = []

    offers = merge_offers([
        *sheet_offers(values),
        *(xentraffic_offers(xentraffic_catalog) if xentraffic_catalog is not None else []),
        *application_ledger_offers(),
        *inventory_programs(census_dir),
    ])
    if xentraffic_catalog is not None:
        reconcile_xentraffic_availability(offers, xentraffic_catalog)
    selection = selection_with_offers(offers)
    selection.update({
        "generated_at_utc": utc_now(),
        "census": census,
        "identity_and_traffic_context": {
            "legal_entity": "Tilton Group LLC",
            "site": "glpclinicscout.com",
            "partner_email": "partners@glpclinicscout.com",
            "publisher_type": "organic editorial search publisher; not a clinic; no clinical recommendations",
            "traffic": "organic editorial search only; no paid arbitrage, incentivized traffic, or coupon injection",
            "disclosure": "clear FTC affiliate disclosure on every monetized placement",
            "standing_exclusions": ["Fella Health: permanently excluded because the terms prohibit review sites"],
            "standing_gates": ["MaxBounty activity requires site deployment plus an operator-approved advertiser display image"],
        },
        "effects": {"live_link_changes": 0, "repoints": 0, "deploys": 0,
                    "applications": 0, "joins": 0, "tracking_link_mints": 0},
    })
    reconciliation = expected_28_reconciliation(values, offers, selection)
    comparison_decisions = baseline_comparison_decisions(selection)

    if first_run:
        switches = [row for row in comparison_decisions if row["switch_staged"]]
    else:
        switches = changed_winners(previous, selection, trigger_scope)

    matrix_path = atomic_json(run_dir / "matrix.json", selection)
    reconciliation_path = atomic_json(run_dir / "28-brand-reconciliation.json", reconciliation)
    comparisons_path = atomic_json(run_dir / "18-duplicate-comparisons.json", {
        "count": len(comparison_decisions), "decisions": comparison_decisions,
    })
    switches_path = atomic_json(run_dir / "switches-staged.json", {
        "schema": "glp.best-terms-switches.v1",
        "generated_at_utc": utc_now(),
        "switches": switches,
        "effects": {"live_link_changes": 0, "repoints": 0, "deploys": 0},
    })
    backlog = stage_backlog(switches, matrix_path, selection) if stage else {
        "added": [], "added_count": 0, "status": "DRY_RUN",
    }
    queue_receipt = drain_queue(
        payout_queue_rows(values, backlog.get("added") or [], switches_path), run_dir
    ) if stage else {"status": "DRY_RUN", "queued": 0, "results": []}

    result = {
        "schema": "glp.best-terms-run.v1",
        "status": "PARTIAL" if (
            census.get("status") == "PARTIAL" or reconciliation.get("status") == "PARTIAL"
        ) else "PASS",
        "generated_at_utc": utc_now(),
        "mode": "approval_trigger" if approval_events else "weekly",
        "weekly_window": weekly_guard,
        "first_run": first_run,
        "matrix": {"path": repo_rel(matrix_path), "sha256": sha256_file(matrix_path),
                   "providers": len(selection.get("providers") or [])},
        "reconciliation": {"path": repo_rel(reconciliation_path), "sha256": sha256_file(reconciliation_path),
                           "expected": reconciliation["expected_brand_count"],
                           "silent_drops": reconciliation["silent_drops"],
                           "selected_winner_count": reconciliation["selected_winner_count"],
                           "exact_winner_rows_recorded": reconciliation["exact_winner_rows_recorded"],
                           "winner_recording_gaps": reconciliation["winner_recording_gaps"],
                           "winner_selection_blocks": reconciliation["winner_selection_blocks"]},
        "comparisons": {"path": repo_rel(comparisons_path), "sha256": sha256_file(comparisons_path),
                        "count": len(comparison_decisions)},
        "switch_backlog": backlog,
        "sheet_queue": queue_receipt,
        "census": census,
        "approval_trigger": {
            "receipt_count": len(trigger_receipts),
            "providers": sorted(trigger_scope or []),
            "receipts": [item["path"] for item in trigger_receipts],
        },
        "effects": selection["effects"],
    }
    run_receipt = atomic_json(run_dir / "run-receipt.json", result)
    result["run_receipt"] = {"path": repo_rel(run_receipt), "sha256": sha256_file(run_receipt)}
    atomic_json(LATEST_MATRIX, selection)
    atomic_json(LATEST_RUN, result)

    if approval_events:
        processed = set(cursor.get("processed_receipts") or [])
        processed.update(item["path"] for item in trigger_receipts)
        atomic_json(APPROVAL_CURSOR, {"processed_receipts": sorted(processed)[-2000:],
                                     "updated_at_utc": utc_now()})
    message = discord_message(result)
    result["discord"] = send_discord(message) if send_alert else {
        "status": "READY" if message else "SILENT", "content": message,
    }
    atomic_json(LATEST_RUN, result)
    if approval_events:
        record_failure_recovery(send_alert=send_alert)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enqueue-payout-leaks", type=Path, metavar="ABSOLUTE_JSON",
        help="Idempotently queue a quantified payout-leak fixture through payout_evaluation.",
    )
    parser.add_argument("--first-run", action="store_true")
    parser.add_argument("--approval-events", action="store_true")
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--census-dir", type=Path)
    parser.add_argument("--xentraffic-catalog", type=Path)
    parser.add_argument("--send-discord", action="store_true")
    parser.add_argument("--stdout", choices=("json", "discord", "silent"), default="json")
    args = parser.parse_args(argv)
    if args.enqueue_payout_leaks:
        if not args.enqueue_payout_leaks.is_absolute():
            parser.error("--enqueue-payout-leaks requires an absolute path")
        try:
            receipt = enqueue_payout_leaks(args.enqueue_payout_leaks)
        except Exception as exc:
            failure = {
                "schema": "glp.payout-leak-deploy-queue-receipt.v1",
                "status": "FAIL",
                "captured_at_utc": utc_now(),
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
                "effects": {"deployments": 0, "live_tracking_changes": 0,
                            "provider_mutations": 0, "tracking_links_opened_or_clicked": 0},
            }
            atomic_json(PAYOUT_LEAK_QUEUE_RECEIPT, failure)
            if args.stdout == "json":
                print(json.dumps(failure, sort_keys=True))
            return 1
        if args.stdout == "json":
            print(json.dumps({
                "status": receipt["status"],
                "rows_expected": receipt["rows_expected"],
                "queued_new": receipt["queued_new"],
                "already_present": receipt["already_present"],
                "enriched_in_place": receipt["enriched_in_place"],
                "blocked": receipt["blocked"],
                "receipt": str(PAYOUT_LEAK_QUEUE_RECEIPT),
            }, sort_keys=True))
        return 0 if receipt["status"] == "PASS" else 1
    try:
        result = run_machine(
            first_run=args.first_run,
            approval_events=args.approval_events,
            stage=args.stage,
            census_dir=args.census_dir,
            xentraffic_catalog=args.xentraffic_catalog,
            send_alert=args.send_discord,
        )
    except Exception as exc:
        failure = {
            "schema": "glp.best-terms-run.v1",
            "status": "FAIL",
            "error_class": type(exc).__name__,
            "error": str(exc)[:500],
            "generated_at_utc": utc_now(),
            "effects": {"live_link_changes": 0, "repoints": 0, "deploys": 0},
        }
        atomic_json(LATEST_RUN, failure)
        notification = record_failure(exc, send_alert=args.send_discord)
        failure["failure_notification"] = notification
        atomic_json(LATEST_RUN, failure)
        if args.stdout == "discord":
            print("Best Terms failure; inspect private run receipt")
        elif args.stdout == "json":
            print(json.dumps(failure, sort_keys=True))
        return 1
    if args.stdout == "discord":
        content = (result.get("discord") or {}).get("content") or discord_message(result)
        if content:
            print(content)
    elif args.stdout == "json":
        print(json.dumps({
            "status": result.get("status"),
            "mode": result.get("mode"),
            "run_receipt": result.get("run_receipt"),
            "switches_staged": (result.get("switch_backlog") or {}).get("added_count", 0),
            "census_status": (result.get("census") or {}).get("status"),
            "sheet_queue_status": (result.get("sheet_queue") or {}).get("status"),
        }, sort_keys=True))
    return 0 if result.get("status") in {
        "PASS", "PASS_NO_NEW_APPROVALS", "PASS_DUPLICATE_WINDOW", "PARTIAL",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
