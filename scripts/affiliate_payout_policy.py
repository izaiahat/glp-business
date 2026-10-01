#!/usr/bin/env python3
"""Permanent cross-network affiliate payout policy.

The policy is pure: it groups exact provider identities across the supported
rails, computes the effective value named by the operator, and returns actions.
It never submits an application, mints a link, edits the site, or writes the
Sheet. Callers persist the evaluation and queue an evidenced row to the one
Sheet writer.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

NETWORKS = frozenset({
    "affiliati", "amazonassociates", "awin", "cj", "clickbank", "clickdealer", "direct",
    "everflow", "flexoffers", "goaffpro", "healthtrader", "impact", "katalys",
    "levanta", "markethealth", "maxbounty", "partnerize", "partnerstack",
    "rakuten", "refersion", "shareasale", "sovrn", "trackdesk", "uppromote",
    "xentraffic", "roeverflowwhitelabeltenant4131",
})
APPROVED_RELATIONSHIPS = frozenset({
    "approved", "active", "joined", "build_tracking_link", "live",
})
APPLYABLE_RELATIONSHIPS = frozenset({
    "notjoined", "not_joined", "not_related", "apply_needed",
    "request_approval", "request_approval_to_run",
})
REJECTED_RELATIONSHIPS = frozenset({"rejected", "declined", "denied"})
PENDING_RELATIONSHIPS = frozenset({"pending", "applied", "under_review", "submitted"})
PERMANENT_EXCLUSIONS = frozenset({"fella health", "fella"})
DEFAULT_MIN_SETTLED = 20
DEFAULT_SWITCH_RATIO = Decimal("1.10")
BUSINESS = Path(__file__).resolve().parents[1]
BEST_TERMS_CONFIG = BUSINESS / "config/affiliate-sites/best-terms-selection.json"


def load_selection_config(path: Path = BEST_TERMS_CONFIG) -> dict[str, Any]:
    """Load and validate the single governed best-terms policy configuration."""
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"best_terms_config_unreadable:{path}") from exc
    if config.get("schema") != "glp.best-terms-selection-config.v1":
        raise ValueError("best_terms_config_schema_invalid")
    if (config.get("rerank") or {}).get("cadence") != "weekly":
        raise ValueError("best_terms_config_weekly_rerank_required")
    priors = config.get("priors") or {}
    expected = {"cpa_sale": Decimal("0.015"), "cpl_lead": Decimal("0.08")}
    for key, required in expected.items():
        row = priors.get(key) or {}
        if decimal_value(row.get("conversion_rate_per_click")) != required:
            raise ValueError(f"best_terms_config_prior_invalid:{key}")
    if not str(config.get("performance_path") or "").strip():
        raise ValueError("best_terms_config_performance_path_required")
    runtime = config.get("collector_runtime") or {}
    if not str(runtime.get("interpreter") or "").strip():
        raise ValueError("best_terms_config_collector_interpreter_required")
    if not runtime.get("required_imports"):
        raise ValueError("best_terms_config_required_imports_required")
    return config


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def network_key(value: Any) -> str:
    key = norm(value)
    aliases = {
        "affiliatinetwork": "affiliati",
        "cjaffiliate": "cj",
        "commissionjunction": "cj",
        "clickbankmarketplace": "clickbank",
        "goaffprodirect": "goaffpro",
        "healthtradernetwork": "healthtrader",
        "impactcom": "impact",
        "impactradius": "impact",
        "maxbountynetwork": "maxbounty",
        "awinaccess": "awin",
        "katalysrevoffers": "katalys",
        "partnerstacknetwork": "partnerstack",
        "rakutenadvertising": "rakuten",
        "refersionmarketplace": "refersion",
        "uppromotemarketplace": "uppromote",
        "xentrafficeverflow": "xentraffic",
    }
    return aliases.get(key, key)


def provider_domain(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = (urlparse(text).hostname or "").casefold().removeprefix("www.")
    network_hosts = (
        "affiliatinetwork.com", "awin.com", "awin1.com", "cj.com", "clickdealer.com", "impact.com",
        "glpclinicscout.com", "katalys.com", "linksynergy.com", "maxbounty.com", "partnerize.com",
        "revoffers.com", "trackdesk.com", "xentraffic.everflowclient.io",
    )
    if not host or any(host == item or host.endswith("." + item) for item in network_hosts):
        return ""
    parts = host.split(".")
    compound_suffixes = {"co.uk", "com.au", "com.br", "com.mx", "co.nz"}
    suffix = ".".join(parts[-2:])
    root = ".".join(parts[-3:]) if suffix in compound_suffixes and len(parts) >= 3 else suffix
    aliases = {"tryshed.com": "shedrx.com"}
    return aliases.get(root, root)


_OFFER_DECORATIONS = re.compile(
    r"\b(?:affiliate\s+program|campaign|offer|program|programme|"
    r"glp\s*-?\s*1|medical\s+weight\s+loss|weight\s+loss|"
    r"cpa|cps|cpl|rev\s*share|revenue\s+share)\b.*$",
    re.I,
)
_RAIL_DECORATIONS = re.compile(
    r"\b(?:affiliati(?:\s+network)?|awin(?:\s+access)?|cj(?:\s+affiliate)?|"
    r"impact(?:\.com)?|katalys|revoffers|maxbounty(?:\s+network)?|"
    r"partnerize|partnerstack|clickdealer|xentraffic)\b",
    re.I,
)
_CORPORATE_SUFFIXES = re.compile(
    r"\b(?:incorporated|inc|llc|ltd|limited|corp|corporation|company|co)\b\.?$",
    re.I,
)
_GEO_DECORATIONS = re.compile(
    r"\b(?:us|usa|united\s+states|ca|canada|uk|united\s+kingdom|au|australia)\b",
    re.I,
)
_PROVIDER_NAME_ALIASES = {
    "gala": "galahealth",
    "galahealth": "galahealth",
    "shed": "shedrx",
    "shedrx": "shedrx",
}


def provider_name_key(value: Any) -> str:
    """Normalize provider identity for analysis without changing Sheet rows."""
    text = str(value or "").strip().casefold()
    text = re.sub(r"\|\s*offer\s*[a-z0-9._-]+.*$", "", text, flags=re.I)
    text = re.sub(r"[\[(]([^\])]+)[\])]", r" \1 ", text)
    text = re.sub(r"\s+[|—–-]\s+", " ", text)
    text = _RAIL_DECORATIONS.sub(" ", text)
    text = _OFFER_DECORATIONS.sub(" ", text)
    text = _GEO_DECORATIONS.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    while _CORPORATE_SUFFIXES.search(text):
        text = _CORPORATE_SUFFIXES.sub("", text).strip()
    key = norm(text)
    return _PROVIDER_NAME_ALIASES.get(key, key)


def provider_domains(record: dict[str, Any]) -> tuple[str, ...]:
    """Return merchant destinations, excluding marketplace/tracking hosts."""
    domains = {
        provider_domain(record.get(field))
        for field in ("official_url", "destination_url", "provider_url", "provider_domain")
    }
    return tuple(sorted(domain for domain in domains if domain))


def provider_identity(record: dict[str, Any]) -> str:
    """Exact, network-free identity. Ambiguity is never silently merged."""
    domains = provider_domains(record)
    if domains:
        return "domain:" + domains[0]
    name = provider_name_key(record.get("provider_name") or record.get("name"))
    return "name:" + name if name else ""


def resolve_provider_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve cross-rail groups with merchant URL evidence stronger than names.

    Shared merchant domains always join. Normalized names join only when their
    explicit domains do not conflict. This batch analysis layer never merges or
    edits provider x marketplace Sheet rows.
    """
    rows = [dict(record) for record in records]
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    names: dict[str, list[int]] = defaultdict(list)
    domains: dict[str, list[int]] = defaultdict(list)
    row_domains: list[tuple[str, ...]] = []
    for index, row in enumerate(rows):
        name = provider_name_key(row.get("provider_name") or row.get("name"))
        row["provider_name_key"] = name
        if name:
            names[name].append(index)
        found_domains = provider_domains(row)
        row_domains.append(found_domains)
        for domain in found_domains:
            domains[domain].append(index)

    for indexes in domains.values():
        for index in indexes[1:]:
            union(indexes[0], index)
    for indexes in names.values():
        explicit = {domain for index in indexes for domain in row_domains[index]}
        if len(explicit) <= 1:
            for index in indexes[1:]:
                union(indexes[0], index)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        groups[find(index)].append(index)
    for indexes in groups.values():
        explicit = sorted({domain for index in indexes for domain in row_domains[index]})
        keys = sorted(
            {str(rows[index].get("provider_name_key") or "") for index in indexes
             if rows[index].get("provider_name_key")},
            key=lambda value: (len(value), value),
        )
        identity = "domain:" + explicit[0] if explicit else ("name:" + keys[0] if keys else "")
        for index in indexes:
            rows[index]["provider_identity"] = identity
            rows[index]["provider_domains"] = list(explicit)
    return rows


def decimal_value(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"payout_value_invalid:{value!r}") from exc
    if result < 0:
        raise ValueError(f"payout_value_negative:{value!r}")
    return result


def performance_key(record: dict[str, Any]) -> tuple[str, str]:
    program_id = (
        record.get("program_id") or record.get("offer_id")
        or record.get("network_program_id") or ""
    )
    return network_key(record.get("network")), str(program_id).casefold()


def effective_value(
    record: dict[str, Any],
    performance: dict[str, Any] | None = None,
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
) -> dict[str, Any]:
    """payout × acceptance × (1 − reversal), or headline before sufficiency."""
    headline = decimal_value(record.get("headline_payout"))
    perf = performance or {}
    settled = int(perf.get("settled_count") or 0)
    if settled < int(min_settled):
        return {
            "basis": "headline",
            "headline_payout": float(headline),
            "lead_acceptance_rate": None,
            "reversal_rate": None,
            "effective_value": float(headline),
            "settled_count": settled,
            "minimum_settled": int(min_settled),
        }

    leads = int(perf.get("lead_count") or 0)
    accepted = int(perf.get("accepted_count") or 0)
    reversals = int(perf.get("reversal_count") or 0)
    if leads <= 0 or accepted < 0 or accepted > leads or reversals < 0 or reversals > accepted:
        raise ValueError(
            f"performance_counts_invalid:leads={leads}:accepted={accepted}:reversals={reversals}"
        )
    acceptance = Decimal(accepted) / Decimal(leads)
    reversal = Decimal(reversals) / Decimal(accepted) if accepted else Decimal("0")
    result = headline * acceptance * (Decimal("1") - reversal)
    return {
        "basis": "measured",
        "headline_payout": float(headline),
        "lead_acceptance_rate": float(acceptance),
        "reversal_rate": float(reversal),
        "effective_value": float(result),
        "lead_count": leads,
        "accepted_count": accepted,
        "settled_count": settled,
        "reversal_count": reversals,
        "minimum_settled": int(min_settled),
    }


def _performance_index(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = performance_key(row)
        if not all(key):
            continue
        if key in out:
            raise ValueError(f"duplicate_performance_record:{key[0]}:{key[1]}")
        out[key] = row
    return out


def conversion_event(record: dict[str, Any]) -> str:
    """Normalize network labels that describe the same paid conversion event."""
    explicit = str(record.get("paid_event") or "").strip().casefold()
    if explicit:
        normalized = re.sub(r"[^a-z0-9]+", "_", explicit).strip("_")
        aliases = {
            "glp_1_acquisition": "glp1_acquisition",
            "glp1_cpa": "glp1_acquisition",
            "medication_subscription": "medication_subscription_signup",
            "subscription_signup": "medication_subscription_signup",
            "booked_appointment": "appointment_booked",
        }
        return aliases.get(normalized, normalized)
    model = str(record.get("payout_model") or "").strip().casefold()
    if model in {"cpl", "lead"}:
        return "lead"
    if model in {
        "cps", "cpa", "fixed", "sale", "action", "acquisition",
        "cpa/sale", "cpa / sale", "cpa/cps", "cpa / cps",
    }:
        return "conversion"
    return model or "unknown"


def comparison_event(record: dict[str, Any]) -> str:
    """Paid-event identity for cross-rail ranking, without inventing equivalence.

    A payout model such as CPA says how a network pays; it does not prove the
    exact event that earns the payout.  When a collector explicitly records that
    it fell back to the payout model, keep that event rail-specific.  This lets
    the matrix preserve an already-live route without ranking two unknown events
    as though they were the same conversion.
    """
    selected = str(record.get("conversion_event") or "").strip()
    if selected:
        return selected
    event = conversion_event(record)
    if str(record.get("paid_event_basis") or "").strip() == "payout_model_fallback":
        network = network_key(record.get("network")) or "unknown-network"
        program = str(record.get("program_id") or "unknown-program").casefold()
        return f"unpublished_exact_event:{network}:{program}:{event}"
    return event


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def working_postback(record: dict[str, Any]) -> bool:
    """Recognize only affirmative postback evidence for an offer tie-break."""
    for field in (
        "working_postback", "postback_working", "postback_verified", "postback_status",
    ):
        value = record.get(field)
        if value is True or str(value or "").strip().casefold() in {
            "true", "yes", "1", "pass", "working", "verified",
        }:
            return True
    return False


def already_live(record: dict[str, Any]) -> bool:
    """Recognize the existing live-route spellings used by policy inputs."""
    for field in ("wired_live", "already_live", "live"):
        value = record.get(field)
        if value is True or str(value or "").strip().casefold() in {
            "true", "yes", "1", "live", "verified",
        }:
            return True
    return False


def _prior_key(record: dict[str, Any], config: dict[str, Any]) -> str | None:
    model = str(record.get("payout_model") or "").strip().casefold()
    for key in ("cpa_sale", "cpl_lead"):
        models = {
            str(value).strip().casefold()
            for value in ((config.get("priors") or {}).get(key) or {}).get("models") or []
        }
        if model in models:
            return key
    event = conversion_event(record)
    if event in {"lead", "qualified_lead"}:
        return "cpl_lead"
    return None


def expected_earnings_per_100_clicks(
    record: dict[str, Any],
    performance: dict[str, Any] | None = None,
    *,
    selection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return auditable cross-event earnings, preferring measured EPC to priors.

    Measured performance is usable after either exact positive click/conversion
    counts or an authenticated network aggregate that proves a positive
    denominator with both EPC and CVR.  The aggregate path never invents counts.
    An explicit measured EPC wins; otherwise actual earnings are divided by
    clicks, with payout x conversions as the documented fallback.
    """
    config = selection_config or load_selection_config()
    payout = decimal_value(record.get("headline_payout"))
    perf = performance or {}
    try:
        clicks = int(perf.get("clicks") or perf.get("click_count") or 0)
        conversions = int(
            perf.get("conversions") or perf.get("conversion_count")
            or perf.get("paid_conversions") or 0
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("performance_clicks_or_conversions_invalid") from exc
    if clicks < 0 or conversions < 0:
        raise ValueError("performance_clicks_or_conversions_negative")

    epc_value = next((
        perf.get(field) for field in ("measured_epc", "epc")
        if perf.get(field) not in (None, "")
    ), None)
    aggregate_cvr_value = next((
        perf.get(field) for field in ("measured_cvr", "cvr")
        if perf.get(field) not in (None, "")
    ), None)
    aggregate_denominator = str(perf.get("denominator_status") or "").strip().upper()
    aggregate_source = str(perf.get("source_classification") or "").strip().upper()
    aggregate_provenance = str(perf.get("provenance") or "").strip()
    aggregate_measured = bool(
        epc_value not in (None, "")
        and aggregate_cvr_value not in (None, "")
        and decimal_value(epc_value) >= 0
        and decimal_value(aggregate_cvr_value) > 0
        and aggregate_denominator == "NETWORK_REPORTED_POSITIVE_DENOMINATOR"
        and aggregate_source in {"NETWORK_REPORTED", "API_REPORTED"}
        and aggregate_provenance
    )

    if (clicks > 0 and conversions > 0) or aggregate_measured:
        earnings_value = next((
            perf.get(field) for field in (
                "earnings", "earnings_usd", "commission_earnings", "commission_usd",
                "commissions", "revenue", "revenue_usd", "gross_earnings_usd",
                "payout_earned",
            ) if perf.get(field) not in (None, "")
        ), None)
        if epc_value not in (None, ""):
            epc = decimal_value(epc_value)
            arithmetic = (
                f"measured EPC {_decimal_text(epc)} * 100 clicks = "
                f"{_decimal_text(epc * Decimal(100))}"
            )
            if aggregate_measured and not (clicks > 0 and conversions > 0):
                earnings_basis = "network_reported_epc_cvr_positive_denominator"
            else:
                earnings_basis = "explicit_measured_epc"
        else:
            earnings = (decimal_value(earnings_value) if earnings_value not in (None, "")
                        else payout * Decimal(conversions))
            epc = earnings / Decimal(clicks)
            arithmetic = (
                f"{_decimal_text(earnings)} earnings / {clicks} clicks = "
                f"{_decimal_text(epc)} EPC; {_decimal_text(epc)} * 100 clicks = "
                f"{_decimal_text(epc * Decimal(100))}"
            )
            earnings_basis = ("measured_earnings" if earnings_value not in (None, "")
                              else "payout_times_measured_conversions")
        per_100 = epc * Decimal(100)
        return {
            "basis": "measured_epc",
            "performance_basis": earnings_basis,
            "clicks": clicks if clicks > 0 else None,
            "conversions": conversions if conversions > 0 else None,
            "measured_epc": float(epc),
            "measured_cvr": (
                float(decimal_value(aggregate_cvr_value))
                if aggregate_cvr_value not in (None, "") else None
            ),
            "measured_provenance": aggregate_provenance or None,
            "prior_conversion_rate_per_click": None,
            "expected_conversions_per_100_clicks": None,
            "expected_earnings_per_100_clicks": float(per_100),
            "arithmetic": arithmetic,
        }

    prior_key = _prior_key(record, config)
    if prior_key is None:
        return {
            "basis": "unknown",
            "performance_basis": None,
            "clicks": clicks,
            "conversions": conversions,
            "measured_epc": None,
            "prior_conversion_rate_per_click": None,
            "expected_conversions_per_100_clicks": None,
            "expected_earnings_per_100_clicks": None,
            "arithmetic": "No configured CPA/sale or CPL/lead prior applies.",
        }
    prior = (config.get("priors") or {})[prior_key]
    rate = decimal_value(prior.get("conversion_rate_per_click"))
    expected_conversions = rate * Decimal(100)
    per_100 = payout * expected_conversions
    return {
        "basis": f"{prior_key}_prior",
        "performance_basis": None,
        "clicks": clicks,
        "conversions": conversions,
        "measured_epc": None,
        "prior_conversion_rate_per_click": float(rate),
        "expected_conversions_per_100_clicks": float(expected_conversions),
        "expected_earnings_per_100_clicks": float(per_100),
        "arithmetic": (
            f"{_decimal_text(payout)} payout * ({_decimal_text(rate)} * 100 clicks) = "
            f"{_decimal_text(payout)} * {_decimal_text(expected_conversions)} = "
            f"{_decimal_text(per_100)}"
        ),
    }


def program_tie_rank(value: Any) -> tuple[int, int | str]:
    """Prefer the stable base/lower numeric offer when all economics tie."""
    text = str(value or "")
    try:
        return 1, -int(text)
    except ValueError:
        return 0, text


def normalized_relationship(value: Any) -> str:
    """Return the small relationship vocabulary used by winner eligibility."""
    relationship = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "approved_active": "approved",
        "approved_active_tracking_ready": "approved",
        "approved_marketplace_access": "approved",
        "application_submitted": "pending",
        "applied_pending_brand_approval": "pending",
        "application_rejected": "rejected",
        "provider_rejected": "rejected",
        "never_joined": "notjoined",
        "notapplied": "notjoined",
        "not_applied": "notjoined",
    }
    return aliases.get(relationship, relationship)


def upgrade_action(record: dict[str, Any]) -> str | None:
    """Return the one labelled action for an offer that cannot win today."""
    relationship = normalized_relationship(record.get("relationship"))
    if relationship in REJECTED_RELATIONSHIPS:
        return "rejected_permanently"
    if relationship in PENDING_RELATIONSHIPS:
        return "awaiting_approval"
    if relationship in APPLYABLE_RELATIONSHIPS or relationship in {"", "unknown"}:
        return "apply"
    if relationship not in APPROVED_RELATIONSHIPS:
        return "apply"
    if not bool(record.get("wire_in_ready") or already_live(record)):
        return "awaiting_approval"
    return None


def traffic_collectible(record: dict[str, Any]) -> tuple[bool, str]:
    """Prove the offer permits GLP Clinic Scout's organic editorial traffic."""
    provider = str(record.get("provider_name") or record.get("provider") or "").strip().casefold()
    if provider in PERMANENT_EXCLUSIONS or bool(record.get("permanently_excluded")):
        return False, "standing_exclusion_terms_prohibit_editorial_review_traffic"
    if record.get("traffic_eligible") is False:
        return False, str(record.get("traffic_exclusion_reason") or "traffic_model_not_permitted")
    exclusion = str(record.get("traffic_exclusion_reason") or "").strip()
    if exclusion:
        return False, exclusion
    return True, "organic_editorial_search_permitted_or_no_exclusion_recorded"


def collectible_eligibility(record: dict[str, Any]) -> dict[str, Any]:
    """Hard gate used immediately before an offer is admitted to winner ranking."""
    network = network_key(record.get("network"))
    relationship = normalized_relationship(record.get("relationship"))
    traffic_ok, traffic_reason = traffic_collectible(record)
    reasons: list[str] = []
    if network not in NETWORKS:
        reasons.append("network_outside_policy")
    if relationship not in APPROVED_RELATIONSHIPS:
        reasons.append(f"relationship_not_approved:{relationship or 'unknown'}")
    if not bool(record.get("wire_in_ready") or already_live(record)):
        reasons.append("not_wire_in_ready")
    if not traffic_ok:
        reasons.append(traffic_reason)
    if not str(record.get('currency') or '').strip():
        reasons.append('currency_unavailable')
    if conversion_event(record) == "unknown":
        reasons.append("payout_model_unclassified")
    try:
        decimal_value(record.get("headline_payout"))
    except ValueError:
        reasons.append("payout_unavailable_or_invalid")
    return {
        "eligible": not reasons,
        "network": network,
        "relationship": relationship,
        "wire_in_ready": bool(record.get("wire_in_ready") or already_live(record)),
        "traffic_collectible": traffic_ok,
        "traffic_basis": traffic_reason,
        "reasons": reasons,
    }


def expected_value_per_lead(
    record: dict[str, Any], performance: dict[str, Any] | None = None,
    *, min_settled: int = DEFAULT_MIN_SETTLED,
) -> dict[str, Any]:
    """Normalize only when the source supplies a valid lead-event assumption.

    RevShare is never converted to CPA. CPA/CPS and CPL stay different events.
    A CPA/CPS row without an explicit lead-to-paid-event rate therefore carries
    a null EV/lead rather than a made-up conversion assumption.
    """
    event = conversion_event(record)
    economics = effective_value(record, performance, min_settled=min_settled)
    effective = decimal_value(economics["effective_value"])
    model = str(record.get("payout_model") or "").strip().casefold()
    if model in {"revshare", "revenue_share", "revenue share", "%"}:
        return {
            "expected_value_per_lead": None,
            "assumption": "RevShare retained as RevShare; no CPA or EV/lead conversion attempted.",
            "conversion_event": event,
            "economics": economics,
        }
    if event == "lead":
        return {
            "expected_value_per_lead": float(effective),
            "assumption": str(record.get("expected_value_assumption") or
                              "One qualified/accepted lead payout event per submitted lead; observed acceptance and reversal factors apply when available."),
            "conversion_event": event,
            "economics": economics,
        }
    if event == "conversion" or bool(str(record.get("paid_event") or "").strip()):
        rate_raw = record.get("lead_to_paid_event_rate")
        if rate_raw in (None, ""):
            return {
                "expected_value_per_lead": None,
                "assumption": "No lead-to-paid-event rate supplied; compare only against offers paying for the same conversion event.",
                "conversion_event": event,
                "economics": economics,
            }
        rate = decimal_value(rate_raw)
        if rate < 0 or rate > 1:
            raise ValueError("lead_to_paid_event_rate_out_of_range")
        assumption = str(record.get("expected_value_assumption") or "").strip()
        if not assumption:
            raise ValueError("expected_value_assumption_required_for_lead_normalization")
        return {
            "expected_value_per_lead": float(effective * rate),
            "assumption": assumption,
            "conversion_event": event,
            "economics": economics,
        }
    return {
        "expected_value_per_lead": None,
        "assumption": "Payout event is unclassified; no normalization attempted.",
        "conversion_event": event,
        "economics": economics,
    }


def _materially_worse_attribution(candidate: dict[str, Any], peers: list[dict[str, Any]]) -> bool:
    """A window at most half the best peer and at least 14 days shorter is material."""
    try:
        days = int(candidate.get("attribution_days") or 0)
    except (TypeError, ValueError):
        return False
    peer_days = []
    for peer in peers:
        try:
            peer_days.append(int(peer.get("attribution_days") or 0))
        except (TypeError, ValueError):
            continue
    longest = max(peer_days or [0])
    return bool(days and longest and longest - days >= 14 and days * 2 <= longest)


def select_best_collectible(
    records: Iterable[dict[str, Any]],
    performance_rows: Iterable[dict[str, Any]] = (),
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
    selection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the best collectible offer under the weekly best-terms policy.

    Offers paying for the same event rank on headline payout. Winners for
    different events rank on expected earnings per 100 clicks, using measured
    EPC when it is qualified and the governed CPA/CPL priors otherwise.
    """
    config = selection_config or load_selection_config()
    perf = _performance_index(performance_rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ignored: list[dict[str, Any]] = []
    for row in resolve_provider_records(records):
        identity = str(row.get("provider_identity") or "")
        if not identity:
            ignored.append({"reason": "provider_identity_unproven",
                            "network": network_key(row.get("network")),
                            "program_id": row.get("program_id")})
            continue
        row["provider_identity"] = identity
        row["network"] = network_key(row.get("network"))
        row["eligibility"] = collectible_eligibility(row)
        grouped[identity].append(row)

    providers: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for identity, rows in sorted(grouped.items()):
        ready = [row for row in rows if row["eligibility"]["eligible"]]
        upgrades = [{
            "network": row["network"],
            "program_id": str(row.get("program_id") or ""),
            "relationship": row["eligibility"]["relationship"],
            "payout_model": row.get("payout_model"),
            "payout": row.get("headline_payout"),
            "conversion_event": comparison_event(row),
            "action": upgrade_action(row),
            "ineligible_reasons": row["eligibility"]["reasons"],
        } for row in rows if not row["eligibility"]["eligible"]]

        by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in ready:
            by_event[(comparison_event(row), str(row.get("currency") or "").strip().upper())].append(row)
        winners_by_event = []
        for (event, currency), peers in sorted(by_event.items()):
            # Same paid event: payout is the deciding economic fact. Postback and
            # live state are only tie-breakers; attribution never suppresses a
            # strictly higher same-event payout.
            winner = max(
                peers,
                key=lambda row: (
                    decimal_value(row.get("headline_payout")),
                    working_postback(row),
                    already_live(row),
                    int(row.get("attribution_days") or 0),
                    row["network"], program_tie_rank(row.get("program_id")),
                ),
            )
            payout = decimal_value(winner.get("headline_payout"))
            expected = expected_earnings_per_100_clicks(
                winner, perf.get(performance_key(winner)), selection_config=config)
            winners_by_event.append({
                "conversion_event": event,
                "paid_event": event,
                "currency": currency,
                "network": winner["network"],
                "program_id": str(winner.get("program_id") or ""),
                "payout_model": winner.get("payout_model"),
                "payout": winner.get("headline_payout"),
                "effective_value": float(payout),
                "expected_earnings_per_100_clicks": expected["expected_earnings_per_100_clicks"],
                "expected_earnings_basis": expected["basis"],
                "performance_basis": expected["performance_basis"],
                "expected_earnings_arithmetic": expected["arithmetic"],
                "measured_epc": expected["measured_epc"],
                "measured_cvr": expected.get("measured_cvr"),
                "measured_provenance": expected.get("measured_provenance"),
                "performance_clicks": expected["clicks"],
                "performance_conversions": expected["conversions"],
                "attribution_days": winner.get("attribution_days"),
                "sheet_row": winner.get("sheet_row"),
                "wired_live": already_live(winner),
                "working_postback": working_postback(winner),
                "wire_in_ready": True,
                "traffic_basis": winner["eligibility"]["traffic_basis"],
                "materially_worse_attribution_rejected": [],
            })

        selected = None
        selection_basis = "no_wire_in_ready_offer"
        if winners_by_event:
            # The governed CPA/CPL priors exist specifically so different or
            # unpublished exact paid events remain rankable without inventing
            # a conversion rate.  Treat a cross-event comparison as unknown
            # only when the EV function itself has no measured/network result
            # and no configured prior.  Merely lacking an exact event label is
            # not authority to keep a lower-paying live route.
            unknown_cross_event = len(winners_by_event) > 1 and any(
                row.get("expected_earnings_per_100_clicks") is None
                for row in winners_by_event
            )
            if unknown_cross_event:
                # The operator's unknown-economics rule is deliberately more
                # conservative than the CPA/CPL prior. A proved postback wins;
                # otherwise retain the already-live route before estimates.
                selected = max(
                    winners_by_event,
                    key=lambda row: (
                        bool(row.get("working_postback")),
                        already_live(row),
                        row.get("expected_earnings_per_100_clicks") is not None,
                        decimal_value(row["expected_earnings_per_100_clicks"])
                        if row.get("expected_earnings_per_100_clicks") is not None else Decimal("0"),
                        decimal_value(row.get("payout")),
                        row["network"], program_tie_rank(row.get("program_id")),
                    ),
                )
            else:
                selected = max(
                    winners_by_event,
                    key=lambda row: (
                        row.get("expected_earnings_per_100_clicks") is not None,
                        decimal_value(row["expected_earnings_per_100_clicks"])
                        if row.get("expected_earnings_per_100_clicks") is not None else Decimal("0"),
                        bool(row.get("working_postback")),
                        already_live(row),
                        decimal_value(row.get("payout")),
                        row["network"], program_tie_rank(row.get("program_id")),
                    ),
                )
            if len(winners_by_event) == 1:
                selection_basis = "single_collectible_event_highest_payout"
            elif unknown_cross_event:
                selection_basis = "unknown_cross_event_tiebreak_postback_then_live"
            else:
                known = [row["expected_earnings_per_100_clicks"] for row in winners_by_event
                         if row.get("expected_earnings_per_100_clicks") is not None]
                if not known:
                    selection_basis = "unknown_cross_event_tiebreak_postback_then_live"
                elif known.count(selected["expected_earnings_per_100_clicks"]) > 1:
                    selection_basis = "expected_earnings_per_100_clicks_tie_postback_then_live"
                else:
                    selection_basis = "highest_expected_earnings_per_100_clicks"
        if selected:
            chosen = [row for row in ready
                      if row["network"] == selected["network"]
                      and str(row.get("program_id") or "") == selected["program_id"]]
            if len(chosen) != 1 or not chosen[0]["eligibility"]["eligible"]:
                violations.append({"provider_identity": identity,
                                   "reason": "winner_not_collectible"})
        cross_rail_comparisons = []
        approved_rows = [row for row in rows
                         if row["eligibility"]["relationship"] in APPROVED_RELATIONSHIPS]
        for index, left in enumerate(approved_rows):
            for right in approved_rows[index + 1:]:
                if left["network"] == right["network"]:
                    continue
                same_currency = bool(str(left.get('currency') or '').strip()) and (
                    str(left.get("currency") or "").strip().upper()
                    == str(right.get("currency") or "").strip().upper()
                )
                same_event = comparison_event(left) == comparison_event(right)
                left_expected = right_expected = None
                if same_currency and not same_event:
                    try:
                        left_expected = expected_earnings_per_100_clicks(
                            left, perf.get(performance_key(left)), selection_config=config)
                        right_expected = expected_earnings_per_100_clicks(
                            right, perf.get(performance_key(right)), selection_config=config)
                    except ValueError:
                        pass
                cross_event_rankable = bool(
                    left_expected and right_expected
                    and left_expected.get("expected_earnings_per_100_clicks") is not None
                    and right_expected.get("expected_earnings_per_100_clicks") is not None
                )
                comparable = same_currency and (same_event or cross_event_rankable)
                cross_rail_comparisons.append({
                    "left": {"network": left["network"],
                             "program_id": str(left.get("program_id") or ""),
                             "conversion_event": comparison_event(left),
                             "payout": left.get("headline_payout")},
                    "right": {"network": right["network"],
                              "program_id": str(right.get("program_id") or ""),
                              "conversion_event": comparison_event(right),
                              "payout": right.get("headline_payout")},
                    "comparable": comparable,
                    "decision": (
                        "COMPARABLE_SAME_PAID_EVENT" if same_currency and same_event
                        else "COMPARABLE_EXPECTED_EARNINGS_PER_100_CLICKS" if cross_event_rankable
                        else "INCOMPARABLE_CURRENCY_OR_UNKNOWN_EARNINGS"
                    ),
                })
        providers.append({
            "provider_identity": identity,
            "provider_name": next((str(row.get("provider_name") or "") for row in rows
                                   if str(row.get("provider_name") or "").strip()), ""),
            "selected_winner": selected,
            "selection_basis": selection_basis,
            "winners_by_event": winners_by_event,
            "wire_in_ready_offer_count": len(ready),
            "upgrade_candidates": upgrades,
            "cross_rail_comparisons": cross_rail_comparisons,
        })

    return {
        "schema": "glp.best-collectible-offer-selection.v1",
        "rule": "only approved, traffic-collectible, wire-in-ready offers may win",
        "rerank_cadence": "weekly",
        "normalization_rule": (
            "same event: highest payout; different events: expected earnings per 100 clicks; "
            "measured EPC with positive exact counts or authenticated network EPC+CVR denominator proof "
            "overrides CPA/sale 1.5% and CPL/lead 8% priors; counts are never invented"
        ),
        "providers": providers,
        "winner_eligibility_violations": violations,
        "ignored": ignored,
    }


def _comparable(incumbent: dict[str, Any], challenger: dict[str, Any]) -> bool:
    return (
        comparison_event(incumbent) == comparison_event(challenger)
        and bool(str(incumbent.get('currency') or '').strip())
        and str(incumbent.get("currency") or "").strip().upper()
        == str(challenger.get("currency") or "").strip().upper()
    )


def evaluate_portfolio(
    records: Iterable[dict[str, Any]],
    performance_rows: Iterable[dict[str, Any]] = (),
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
    switch_ratio: Decimal | float | str = DEFAULT_SWITCH_RATIO,
) -> dict[str, Any]:
    """Evaluate every exact cross-network provider collision in one pass."""
    ratio_floor = decimal_value(switch_ratio)
    perf = _performance_index(performance_rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ignored: list[dict[str, Any]] = []

    for row in resolve_provider_records(records):
        network = network_key(row.get("network"))
        identity = str(row.get("provider_identity") or "")
        if network not in NETWORKS:
            ignored.append({"reason": "network_outside_policy", "network": network,
                            "program_id": row.get("program_id")})
            continue
        if not identity:
            ignored.append({"reason": "provider_identity_unproven", "network": network,
                            "program_id": row.get("program_id")})
            continue
        row["network"] = network
        row["provider_identity"] = identity
        grouped[identity].append(row)

    decisions: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for identity, rows in sorted(grouped.items()):
        live = [row for row in rows if already_live(row)]
        if len(live) > 1:
            violations.append({
                "provider_identity": identity,
                "reason": "multiple_live_links_for_one_provider",
                "live": [{"network": row["network"], "program_id": row.get("program_id")}
                         for row in live],
            })
            continue
        if not live:
            continue
        incumbent = live[0]
        incumbent_econ = effective_value(
            incumbent, perf.get(performance_key(incumbent)), min_settled=min_settled)
        for challenger in rows:
            if challenger is incumbent or challenger["network"] == incumbent["network"]:
                continue
            relationship = str(challenger.get("relationship") or "").casefold().replace(" ", "_")
            base = {
                "provider_identity": identity,
                "provider_name": challenger.get("provider_name") or incumbent.get("provider_name"),
                "incumbent": {
                    "network": incumbent["network"],
                    "program_id": str(incumbent.get("program_id") or ""),
                    "conversion_event": conversion_event(incumbent),
                    "economics": incumbent_econ,
                    "attribution_days": incumbent.get("attribution_days"),
                    "sheet_row": incumbent.get("sheet_row"),
                    "page_slug": incumbent.get("page_slug"),
                    "suggested_page": incumbent.get("suggested_page"),
                },
                "challenger": {
                    "network": challenger["network"],
                    "program_id": str(challenger.get("program_id") or ""),
                    "conversion_event": conversion_event(challenger),
                    "relationship": relationship,
                    "attribution_days": challenger.get("attribution_days"),
                    "sheet_row": challenger.get("sheet_row"),
                    "detail_url": challenger.get("detail_url"),
                    "landing_pages": challenger.get("landing_pages") or [],
                },
            }
            if relationship in REJECTED_RELATIONSHIPS:
                decisions.append({**base, "decision": "KEEP_INCUMBENT_CHALLENGER_REJECTED"})
                continue
            try:
                challenger_econ = effective_value(
                    challenger, perf.get(performance_key(challenger)), min_settled=min_settled)
            except ValueError as exc:
                decisions.append({**base, "decision": "BLOCKED_INVALID_ECONOMICS",
                                  "reason": str(exc)})
                continue
            base["challenger"]["economics"] = challenger_econ
            if not _comparable(incumbent, challenger):
                decisions.append({**base, "decision": "BLOCKED_INCOMPARABLE_PAYOUT_MODELS",
                                  "reason": "conversion event or currency differs"})
                continue

            inc_value = decimal_value(incumbent_econ["effective_value"])
            chal_value = decimal_value(challenger_econ["effective_value"])
            if chal_value <= inc_value:
                # A sub-threshold challenger never causes link churn, but §7.2
                # still requires the longer cookie to be the recorded preference
                # when the two values are within the 10% noise band. Recording
                # that preference matters for later placement selection without
                # weakening the material-margin switch floor.
                within_floor = (chal_value > 0 and
                                (inc_value / chal_value) <= ratio_floor)
                if relationship in APPROVED_RELATIONSHIPS and within_floor:
                    incumbent_window = int(incumbent.get("attribution_days") or 0)
                    challenger_window = int(challenger.get("attribution_days") or 0)
                    preferred = (challenger["network"] if challenger_window > incumbent_window
                                 else incumbent["network"])
                    decisions.append({
                        **base,
                        "decision": "WITHIN_10_PERCENT_NO_ECONOMIC_SWITCH",
                        "longer_attribution_preference": preferred,
                    })
                    continue
                decisions.append({**base, "decision": "KEEP_INCUMBENT_HIGHER_EFFECTIVE_VALUE"})
                continue
            if relationship in APPLYABLE_RELATIONSHIPS:
                decisions.append({**base, "decision": "APPLY_TO_HIGHER_PAYING_CHALLENGER"})
                continue
            if relationship not in APPROVED_RELATIONSHIPS:
                decisions.append({**base, "decision": "BLOCKED_RELATIONSHIP_UNCLASSIFIED"})
                continue

            economic_ratio = chal_value / inc_value if inc_value else Decimal("Infinity")
            base["challenger"]["advantage_ratio"] = (
                float(economic_ratio) if economic_ratio.is_finite() else "infinity")
            if economic_ratio >= ratio_floor:
                decisions.append({**base, "decision": "SWITCH_ATOMIC_AFTER_LINK_VALIDATION"})
                continue

            incumbent_window = int(incumbent.get("attribution_days") or 0)
            challenger_window = int(challenger.get("attribution_days") or 0)
            preferred = challenger["network"] if challenger_window > incumbent_window else incumbent["network"]
            decisions.append({
                **base,
                "decision": "WITHIN_10_PERCENT_NO_ECONOMIC_SWITCH",
                "longer_attribution_preference": preferred,
            })

    return {
        "schema": "glp.affiliate-payout-evaluation.v1",
        "formula": "effective_value = payout * lead_acceptance_rate * (1 - reversal_rate)",
        "minimum_settled_conversions": int(min_settled),
        "switch_advantage_ratio": float(ratio_floor),
        "provider_groups": len(grouped),
        "decisions": decisions,
        "identity_violations": violations,
        "ignored": ignored,
        "effects": {
            "applications_submitted": 0,
            "tracking_links_minted": 0,
            "provider_deploys": 0,
            "sheet_writes": 0,
        },
    }


def switch_permission(decision: dict[str, Any], *, challenger_link_valid: bool) -> dict[str, Any]:
    """The atomic-swap guard. Validation failure always preserves the incumbent."""
    if decision.get("decision") != "SWITCH_ATOMIC_AFTER_LINK_VALIDATION":
        return {"allowed": False, "action": "KEEP_INCUMBENT", "reason": "decision_not_switchable"}
    if not challenger_link_valid:
        return {"allowed": False, "action": "KEEP_INCUMBENT",
                "reason": "challenger_link_validation_failed"}
    return {
        "allowed": True,
        "action": "ATOMIC_SWAP_REQUIRED",
        "precondition": "one release replaces the incumbent route with the validated challenger",
        "forbidden_intermediate_states": ["two_live_links", "zero_live_links"],
    }


__all__ = [
    "NETWORKS", "APPROVED_RELATIONSHIPS", "APPLYABLE_RELATIONSHIPS",
    "REJECTED_RELATIONSHIPS", "PENDING_RELATIONSHIPS", "PERMANENT_EXCLUSIONS",
    "DEFAULT_MIN_SETTLED", "DEFAULT_SWITCH_RATIO",
    "provider_identity", "provider_name_key", "provider_domains", "resolve_provider_records",
    "effective_value", "evaluate_portfolio", "switch_permission",
    "network_key", "provider_domain", "normalized_relationship", "upgrade_action",
    "traffic_collectible", "collectible_eligibility", "expected_value_per_lead",
    "expected_earnings_per_100_clicks", "working_postback", "already_live",
    "comparison_event",
    "load_selection_config", "select_best_collectible",
]
