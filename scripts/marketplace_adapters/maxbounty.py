#!/usr/bin/env python3
"""MaxBounty authenticated-portal adapter.

MaxBounty has no publisher API for this surface. This adapter logs in through the
existing profile-backed portal lane, enumerates UI search and categories read-only,
and classifies each campaign from the exact action button on its detail page.
The two mutation capabilities are present but hard-gated by the canonical local
policy. There is no force flag and no environment override.
"""
from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import os
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from . import maxbounty_discovery as discovery

BUSINESS = Path("/home/ubuntu/business")
DEFAULT_GATE_POLICY = BUSINESS / "config/affiliate-sites/maxbounty-lane.json"
TRACKING_RETRIEVAL_AUTH_SCHEMA = "glp.maxbounty.tracking-retrieval-authorization.v1"
TRAFFIC_TYPE = "Native"
CREATIVE_ANSWER = "Make my own"
LEADS_PER_DAY = "25"
BUTTON_APPROVED = "Build tracking link"
BUTTON_APPLY = "Request approval to run"
RELATIONSHIP_EVIDENCE_PENDING = "Request under review"
RELATIONSHIP_EVIDENCE_DENIED = "Request denied"
RELATIONSHIP_EVIDENCE_CAPACITY_BLOCKED = "Request capacity blocked"
REL_APPROVED = "approved"
REL_APPLY_NEEDED = "apply_needed"
REL_PENDING = "pending"
REL_DENIED = "denied"
REL_CAPACITY_BLOCKED = "capacity_blocked"
STATUS_SUBMITTED = "SUBMITTED_VERIFIED"
STATUS_TERMINAL_UNAVAILABLE = "TERMINAL_PROVIDER_UNAVAILABLE"
STATUS_TERMINAL_ALREADY_APPROVED = "TERMINAL_ALREADY_APPROVED"
STATUS_PENDING_NO_ATTEMPT = "PROVIDER_STATE_PENDING_NO_ATTEMPT"
STATUS_PROVIDER_CAPACITY_BLOCKED = "PROVIDER_CAPACITY_BLOCKED_NO_ATTEMPT"

# Grounded against the authenticated campaign-28963 DOM captured 2026-08-28.
# The modal's visible prose is not associated with the controls via <label for>,
# so get_by_label() cannot address these fields. Their provider-owned names are
# the stable machine contract.
APPLICATION_DIALOG = "[role='dialog']:visible"
APPLICATION_FIELDS = {
    "leads_per_day": "input[name='estimated_leads']",
    "creative": "select[name='creative_choice']",
    "advertising_image": "input[name='upload_creative'][type='file']",
    "traffic_type": "select[name='traffic_type']",
    "campaign_description": "textarea[name='description']",
}
NATIVE_APPLICATION_FIELDS = {
    "native_traffic_network": "[name='form_source_answer_1']",
    "native_ads": "[name='form_source_answer_2']",
    "custom_landing_page": "[name='form_source_answer_3']",
}
NATIVE_TRAFFIC_NETWORK = (
    "GLP Clinic Scout (Tilton Group LLC), a first-party editorial publisher; "
    "no third-party native ad network."
)
NATIVE_ADS = (
    "Original provider reviews, comparisons, cost guides, and educational placements "
    "with clear FTC affiliate disclosure and no unsupported medical claims."
)
CANONICAL_BUSINESS_IDENTITY = "Tilton Group LLC"
APPLICATION_IDENTITY_CONFIG = BUSINESS / "config/affiliate-sites/glp-clinic-scout.json"

CORE_PATTERNS = (
    ("glp1", r"\bglp[\s-]?1s?\b"),
    ("semaglutide", r"\bsemaglutide\b"),
    ("tirzepatide", r"\btirzepatide\b"),
    ("liraglutide", r"\bliraglutide\b"),
    ("retatrutide", r"\bretatrutide\b"),
    ("medical weight loss", r"\bmedical weight[\s-]?loss\b"),
    ("weight management", r"\bweight management\b"),
)
SISTER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("mens-health-trt", "TRT / men's health", r"\b(?:trt|testosterone|men'?s health)\b"),
    ("mens-health-ed", "ED / men's health", r"\b(?:erectile dysfunction|men'?s sexual health)\b"),
    ("mens-health-hair", "hair loss", r"\b(?:hair loss|finasteride|minoxidil)\b"),
    ("womens-health", "women's health", r"\b(?:women'?s health|menopause|fertility)\b"),
    ("diagnostics-testing", "diagnostics / testing", r"\b(?:diagnostic|lab test|blood test|screening)\b"),
    ("peptides-longevity", "peptides / longevity", r"\b(?:peptide|longevity|nad\+?)\b"),
    ("supplements-nutrition", "supplements / nutrition", r"\b(?:supplement|vitamin|nutrition)\b"),
    ("skincare-aesthetics", "skincare / aesthetics", r"\b(?:skincare|aesthetic|dermatolog)\w*\b"),
    ("fitness-equipment", "fitness", r"\b(?:fitness|workout|gym equipment)\b"),
    ("general-wellness", "general telehealth / wellness", r"\b(?:telehealth|telemedicine|virtual care|wellness)\b"),
)
COUPON_CODE = re.compile(r"\b[A-Z][A-Z0-9_-]{2,19}\d[A-Z0-9_-]*\b")
MONEY = re.compile(r"(?:(?P<symbol>\$)\s*(?P<usd>[0-9]+(?:\.[0-9]{1,2})?)|(?P<num>[0-9]+(?:\.[0-9]{1,2})?)\s*(?P<currency>USD|CAD|EUR|GBP))", re.I)


class MaxBountyError(RuntimeError):
    pass


class GateClosed(MaxBountyError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _provider_valid_leads_per_day(value: Any) -> bool:
    """Match MaxBounty's stricter JS rule: one integer, never below 25."""
    text = str(value or "").strip()
    return text.isdecimal() and int(text) >= 25


def _attempt_receipt(path: Path | None, payload: dict[str, Any]) -> str:
    """Durably journal the provider-side submit boundary without a retry path."""
    if path is None:
        return ""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    raw = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
    with suppress(OSError):
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return sha256_file(path)


def _repo_file(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text or ".." in Path(text).parts:
        return None
    path = Path(text)
    return path if path.is_absolute() else BUSINESS / path


def load_policy(path: Path = DEFAULT_GATE_POLICY) -> dict[str, Any]:
    policy = json.loads(Path(path).read_text())
    if policy.get("schema") != "glp.maxbounty.gate-policy.v1":
        raise MaxBountyError(f"maxbounty_gate_schema_invalid:{policy.get('schema')}")
    if policy.get("default_closed") is not True:
        raise MaxBountyError("maxbounty_gate_must_be_default_closed")
    return policy


def _hash_bound_file(block: dict[str, Any], path_field: str, sha_field: str) -> tuple[bool, str]:
    path = _repo_file(block.get(path_field))
    expected = str(block.get(sha_field) or "").strip().casefold()
    if path is None:
        return False, f"{path_field}_missing"
    if not path.is_file():
        return False, f"{path_field}_not_found"
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        return False, f"{sha_field}_invalid"
    if sha256_file(path) != expected:
        return False, f"{sha_field}_drift"
    return True, "hash_bound"


def mutation_gate_status(policy: dict[str, Any]) -> dict[str, Any]:
    """One gate for applications, tracking-link mints, and MaxBounty deploys."""
    reasons: list[str] = []
    site = policy.get("site_redesign") or {}
    if site.get("live") is not True:
        reasons.append("site_redesign_not_live")
    else:
        ok, reason = _hash_bound_file(site, "verification_receipt_path",
                                      "verification_receipt_sha256")
        if not ok:
            reasons.append("site_redesign_" + reason)

    image = policy.get("ad_placement_image") or {}
    if image.get("operator_approved") is not True:
        reasons.append("ad_placement_image_not_operator_approved")
    if str(image.get("approved_by") or "").strip().casefold() != "operator":
        reasons.append("ad_placement_image_approver_not_operator")
    ok, reason = _hash_bound_file(image, "path", "sha256")
    if not ok:
        reasons.append("ad_placement_image_" + reason)
    return {
        "open": not reasons,
        "status": "OPEN" if not reasons else "CLOSED",
        "reasons": reasons,
        "default_closed": True,
    }


def require_mutation_gate(policy_path: Path = DEFAULT_GATE_POLICY) -> dict[str, Any]:
    status = mutation_gate_status(load_policy(Path(policy_path)))
    if not status["open"]:
        raise GateClosed("maxbounty_mutation_gate_closed:" + ",".join(status["reasons"]))
    return status


def require_tracking_retrieval_authorization(
    authorization_path: Path, program_id: str,
) -> dict[str, Any]:
    """Validate one operator-scoped tracking-link retrieval authorization.

    The canonical MaxBounty gate remains the authority for applications,
    deployments, and ordinary link mints.  This narrower contract exists only
    for a named, already-approved campaign when the operator explicitly orders
    capture of its provider-issued link.  It cannot authorize an application,
    a deploy, or navigation to the resulting tracking URL.
    """
    path = Path(authorization_path)
    if not path.is_absolute():
        path = BUSINESS / path
    path = path.resolve()
    if not path.is_file():
        raise GateClosed("maxbounty_tracking_retrieval_authorization_missing")
    try:
        authorization = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateClosed("maxbounty_tracking_retrieval_authorization_invalid_json") from exc
    expected_program = str(program_id or "").strip()
    allowed_actions = authorization.get("allowed_actions")
    if (
        authorization.get("schema") != TRACKING_RETRIEVAL_AUTH_SCHEMA
        or authorization.get("status") != "APPROVED"
        or str(authorization.get("authorized_by") or "").strip().casefold() != "operator"
        or str(authorization.get("network") or "").strip().casefold() != "maxbounty"
        or str(authorization.get("program_id") or "").strip() != expected_program
        or allowed_actions != ["retrieve_tracking_link"]
        or int(authorization.get("applications_allowed", -1)) != 0
        or int(authorization.get("deployments_allowed", -1)) != 0
        or int(authorization.get("tracking_clicks_allowed", -1)) != 0
        or authorization.get("follow_tracking_url_allowed") is not False
    ):
        raise GateClosed("maxbounty_tracking_retrieval_authorization_scope_invalid")
    expires = str(authorization.get("expires_at_utc") or "").strip()
    try:
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateClosed("maxbounty_tracking_retrieval_authorization_expiry_invalid") from exc
    if expires_at <= datetime.now(timezone.utc):
        raise GateClosed("maxbounty_tracking_retrieval_authorization_expired")
    return {
        "status": "OPEN_SCOPED_RETRIEVAL",
        "program_id": expected_program,
        "authorization_path": str(path),
        "authorization_sha256": sha256_file(path),
    }


def rollout_status(policy: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    gate = mutation_gate_status(policy)
    if not gate["open"]:
        return {"phase": "BLOCKED_PRELIVE_GATE", "allowed_providers": [], "gate": gate}
    rollout = policy.get("rollout") or {}
    test_provider = str(rollout.get("test_provider_id") or "").strip()
    if not test_provider:
        return {"phase": "BLOCKED_TEST_PROVIDER_NOT_REGISTERED", "allowed_providers": []}
    runtime = runtime or {}
    verdict = str(rollout.get("operator_verdict") or "pending").strip().casefold()
    # Operator override (2026-09-01): an explicit, hash-bound "good" verdict releases the full
    # approved backlog even before the single test provider has a deploy receipt. The
    # test-provider step was a Fable-imposed caution, not a legality gate; the operator's
    # recorded instruction supersedes it. "not_good"/invalid verdicts still follow the
    # ordered path below.
    if verdict == "good":
        ok, _reason = _hash_bound_file(rollout, "operator_verdict_receipt_path",
                                       "operator_verdict_receipt_sha256")
        if ok:
            return {"phase": "ALLOW_FULL_BACKLOG", "allowed_providers": ["*"]}
    deployed = runtime.get("test_deploy") or {}
    deployed_ok, _deployed_reason = _hash_bound_file(
        deployed, "receipt_path", "receipt_sha256")
    if str(deployed.get("provider_id") or "") != test_provider or not deployed_ok:
        return {"phase": "ALLOW_ONE_TEST_PROVIDER", "allowed_providers": [test_provider]}

    if verdict == "pending":
        return {"phase": "AWAIT_OPERATOR_TEST_REVIEW", "allowed_providers": []}
    ok, reason = _hash_bound_file(rollout, "operator_verdict_receipt_path",
                                  "operator_verdict_receipt_sha256")
    if not ok:
        return {"phase": "BLOCKED_OPERATOR_VERDICT_" + reason.upper(),
                "allowed_providers": []}
    if verdict == "good":
        return {"phase": "ALLOW_FULL_BACKLOG", "allowed_providers": ["*"]}
    if verdict == "not_good":
        return {"phase": "BLOCKED_FIX_AND_RECONFIRM", "allowed_providers": []}
    return {"phase": "BLOCKED_OPERATOR_VERDICT_INVALID", "allowed_providers": []}


def classify_button(button_texts: Iterable[Any]) -> str:
    texts = {" ".join(str(value or "").split()).casefold() for value in button_texts}
    approved = BUTTON_APPROVED.casefold() in texts
    apply_needed = BUTTON_APPLY.casefold() in texts
    if approved and apply_needed:
        raise MaxBountyError("maxbounty_relationship_buttons_ambiguous")
    if approved:
        return REL_APPROVED
    if apply_needed:
        return REL_APPLY_NEEDED
    raise MaxBountyError("maxbounty_relationship_button_unclassified")


def classify_vertical(text: Any) -> dict[str, Any]:
    haystack = " ".join(str(text or "").split())
    for token, pattern in CORE_PATTERNS:
        hit = re.search(pattern, haystack, re.I)
        if hit:
            return {
                "fit_classification": "core_glp1",
                "vertical": "glp1-weightloss",
                "vertical_name": "GLP-1 / medical weight loss",
                "evidence": hit.group(0),
                "fit_reason": (
                    f"MaxBounty's own campaign detail names {hit.group(0)!r}, directly matching "
                    "GLP Clinic Scout's GLP-1 and medical weight-loss comparison audience."
                ),
            }
    for vertical, label, pattern in SISTER_PATTERNS:
        hit = re.search(pattern, haystack, re.I)
        if hit:
            return {
                "fit_classification": "sister_vertical",
                "vertical": vertical,
                "vertical_name": label,
                "evidence": hit.group(0),
                "fit_reason": (
                    f"MaxBounty's own campaign detail names {hit.group(0)!r}; this is the "
                    f"{label} sister vertical, not a GLP Clinic Scout deployment target."
                ),
            }
    return {
        "fit_classification": "out_of_scope",
        "vertical": "out-of-scope",
        "vertical_name": "out of scope",
        "evidence": "no GLP-1 or supported sister-vertical term found",
        "fit_reason": (
            "The captured MaxBounty campaign detail contains no GLP-1, medical weight-loss, "
            "or supported sister-vertical evidence. It stays on HOLD and is not applied to."
        ),
    }


def payout_details(text: Any, campaign_name: Any = "") -> dict[str, Any]:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines() if line.strip()]
    ranked = [line for line in lines if re.search(r"\b(payout|cpl|cpa|commission|per lead)\b", line, re.I)]
    ranked.extend(line for line in lines if line not in ranked)
    for line in ranked:
        match = MONEY.search(line)
        if not match:
            continue
        value = match.group("usd") or match.group("num")
        currency = "USD" if match.group("symbol") else str(match.group("currency") or "USD").upper()
        low = f"{campaign_name} {line}".casefold()
        if "cpl" in low:
            model = "CPL"
        elif "cps" in low:
            model = "CPS"
        elif "cpa" in low or "per acquisition" in low:
            model = "CPA"
        elif re.search(r"\bsale\b", low):
            model = "CPS"
        elif "per lead" in low:
            model = "CPL"
        else:
            model = "fixed"
        return {"headline_payout": float(value), "currency": currency,
                "payout_model": model, "payout_terms": line[:500]}
    return {"headline_payout": None, "currency": "", "payout_model": "unknown",
            "payout_terms": "Not exposed in captured detail"}


def parse_attribution_days(text: Any) -> int | None:
    match = re.search(r"\b(?:cookie|attribution)(?:\s+window|\s+period)?\D{0,30}(\d{1,4})\s*days?\b",
                      str(text or ""), re.I)
    return int(match.group(1)) if match else None


def parse_coupons(text: Any) -> list[dict[str, str]]:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines() if line.strip()]
    coupons: dict[str, dict[str, str]] = {}
    ignored = {"USD", "CAD", "EUR", "GBP", "CPA", "CPL", "MAXBOUNTY"}
    for index, line in enumerate(lines):
        matches = [match for match in COUPON_CODE.finditer(line.upper())
                   if match.group(0) not in ignored]
        for position, match in enumerate(matches):
            code = match.group(0)
            if code in ignored:
                continue
            end = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            terms = line[match.end():end].strip(" :;-–—")
            terms = re.sub(r"^for\s+", "", terms, flags=re.I)
            if not terms:
                following = []
                for candidate in lines[index + 1:]:
                    if COUPON_CODE.search(candidate.upper()):
                        break
                    if candidate.casefold() != "coupon":
                        following.append(candidate)
                terms = " ".join(following).strip()
            coupons.setdefault(code, {
                "code": code,
                "terms": (terms or "Terms not separately exposed in the Coupon section")[:500],
            })
    return sorted(coupons.values(), key=lambda row: row["code"])


def program_id_from_url(url: Any) -> str:
    path = urlparse(str(url or "")).path
    matches = re.findall(r"(?<![A-Za-z0-9])([A-Za-z0-9_-]{3,})(?![A-Za-z0-9])", path)
    excluded = {"campaign", "campaigns", "browse", "bookmarked", "details", "offers"}
    for value in reversed(matches):
        if value.casefold() not in excluded:
            return value
    query = urlparse(str(url or "")).query
    match = re.search(r"(?:campaign|offer|id)=([A-Za-z0-9_-]+)", query, re.I)
    return match.group(1) if match else ""


def campaign_description(program_name: Any) -> str:
    name = " ".join(str(program_name or "this campaign").split())[:80]
    text = (
        "GLP Clinic Scout is an independent comparison and education publisher built for "
        "high-intent consumers actively evaluating telehealth care, medical weight-loss "
        "programs, cash-pay pricing, and related health services. We will promote "
        f"{name} through original native placements on the most relevant provider review, "
        "comparison, cost, and educational pages, matching the offer to reader intent instead "
        "of using generic traffic. Our editorial system combines detailed program research, "
        "transparent pricing and terms, strong calls to action, per-page Sub ID attribution, "
        "and clear FTC affiliate disclosure. Campaign creative will be made in-house to match "
        "each placement and the operator-approved site design. We use no unsupported medical "
        "claims and will follow all campaign restrictions. This gives the campaign a focused "
        "acquisition channel with measurable page-level attribution."
    )
    if len(text) > 1000:
        raise MaxBountyError("maxbounty_campaign_description_over_1000_characters")
    return text


def custom_landing_page(program: dict[str, Any]) -> str:
    """Return a truthful first-party page without opening a campaign destination link."""
    if str(program.get("program_id") or "") == "28778":
        return "https://glpclinicscout.com/providers/shedrx/"
    return "https://glpclinicscout.com/providers/"


def application_draft(program: dict[str, Any]) -> dict[str, Any]:
    return {
        "program_id": str(program.get("program_id") or ""),
        "program_name": str(program.get("name") or ""),
        "leads_per_day": LEADS_PER_DAY,
        "creative": CREATIVE_ANSWER,
        "traffic_type": TRAFFIC_TYPE,
        "native_traffic_network": NATIVE_TRAFFIC_NETWORK,
        "native_ads": NATIVE_ADS,
        "custom_landing_page": custom_landing_page(program),
        "campaign_description": campaign_description(program.get("name")),
        "ad_placement_image": "operator-approved image from gate policy only",
        "status": "DRAFT_GATE_CLOSED",
    }


def sub_ids(page_slug: Any) -> dict[str, str]:
    slug = re.sub(r"[^a-z0-9]+", "-", str(page_slug or "site").casefold()).strip("-")[:48] or "site"
    return {"sub_id_1": f"glp-scout-{slug}", "sub_id_2": "", "sub_id_3": "", "sub_id_4": ""}


def tracking_configuration(program: dict[str, Any], *, page_slug: str,
                           landing_page: str, creative: str) -> dict[str, Any]:
    if not str(landing_page).startswith("https://"):
        raise MaxBountyError("maxbounty_landing_page_must_be_https")
    if not str(creative).strip():
        raise MaxBountyError("maxbounty_creative_required")
    return {
        "program_id": str(program.get("program_id") or ""),
        "program_name": str(program.get("name") or ""),
        "traffic_type": TRAFFIC_TYPE,
        "creative": str(creative).strip(),
        "landing_page": landing_page,
        "sub_ids": sub_ids(page_slug),
    }


def _button_texts(page: Any) -> list[str]:
    return page.locator("button, [role='button'], a").evaluate_all(
        "els => els.map(e => (e.innerText || e.textContent || '').trim()).filter(Boolean)")


def _relationship_from_page(driver: Any, page: Any, wait_attempts: int = 10) -> tuple[str, list[str]]:
    """Wait for the campaign's relationship control, not merely the page shell."""
    buttons: list[str] = []
    wanted = {BUTTON_APPROVED.casefold(), BUTTON_APPLY.casefold()}
    for attempt in range(wait_attempts + 1):
        buttons = _button_texts(page)
        normalized = {" ".join(value.split()).casefold() for value in buttons}
        if normalized & wanted:
            return classify_button(buttons), buttons
        if attempt < wait_attempts:
            driver.wait(1000)
    return classify_button(buttons), buttons


def _page_links(page: Any) -> list[dict[str, str]]:
    return page.locator("a[href]").evaluate_all(
        r"""els => els.map(e => {
          let text = (e.innerText || e.textContent || '').trim();
          if (!text && /\/campaign\//i.test(e.getAttribute('href') || '')) {
            const card = e.closest('.Campaigncard');
            const name = card && card.querySelector('.font-weight-bold');
            text = ((name && (name.innerText || name.textContent)) || '').trim();
          }
          return {text, href:e.href || ''};
        })""")


def _section_text(page: Any, heading: str) -> str:
    return str(page.evaluate(
        """heading => {
          const wanted = heading.toLowerCase();
          const nodes = [...document.querySelectorAll(
            'h1,h2,h3,h4,h5,h6,[role=heading],dt,strong,[class*=card-header]')];
          const h = nodes.find(el => (el.innerText || el.textContent || '').trim().toLowerCase() === wanted);
          if (!h) return '';
          const box = h.closest('.card,section,article,[class*=panel],[class*=section]') || h.parentElement;
          return (box && (box.innerText || box.textContent) || '').trim();
        }""", heading) or "")


def _section_links(page: Any, heading: str) -> list[str]:
    return list(page.evaluate(
        """heading => {
          const wanted = heading.toLowerCase();
          const nodes = [...document.querySelectorAll(
            'h1,h2,h3,h4,h5,h6,[role=heading],dt,strong,[class*=card-header]')];
          const h = nodes.find(el => (el.innerText || el.textContent || '').trim().toLowerCase() === wanted);
          if (!h) return [];
          const box = h.closest('.card,section,article,[class*=panel],[class*=section]') || h.parentElement;
          return [...(box ? box.querySelectorAll('a[href]') : [])].map(a => a.href || '').filter(Boolean);
        }""", heading) or [])


def open_bookmarked(driver: Any) -> str:
    """Navigate Browse -> Bookmarked without assuming a private URL."""
    page = driver.page
    for label in ("Browse",):
        locator = page.get_by_text(label, exact=True)
        if locator.count():
            locator.first.click()
            driver.wait(1800)
            break
    bookmarked = page.get_by_text("Bookmarked", exact=True)
    if bookmarked.count():
        bookmarked.first.click()
        driver.wait(2500)
    if "bookmark" not in page.url.casefold():
        links = [row for row in _page_links(page)
                 if "bookmark" in (row.get("text", "") + " " + row.get("href", "")).casefold()]
        if links:
            page.goto(links[0]["href"], wait_until="domcontentloaded")
            driver.wait(2500)
    if "bookmark" not in page.url.casefold() and not page.get_by_text("Bookmarked", exact=True).count():
        raise MaxBountyError("maxbounty_bookmarked_surface_not_found")
    return page.url


def bookmarked_campaign_links(driver: Any, max_pages: int = 20) -> list[dict[str, str]]:
    page = driver.page
    seen: dict[str, dict[str, str]] = {}
    for _ in range(max_pages):
        for row in _page_links(page):
            href = str(row.get("href") or "")
            text = " ".join(str(row.get("text") or "").split())
            low = href.casefold()
            if not text or "maxbounty" not in (urlparse(href).hostname or ""):
                continue
            if not re.search(r"/(?:campaigns?|offers?)/", low) and not re.search(
                    r"[?&](?:campaign|offer|id)=", low):
                continue
            seen.setdefault(href, {"name": text[:200], "detail_url": href})
        next_button = page.get_by_role("button", name=re.compile(r"^(?:next|>)$", re.I))
        if not next_button.count() or not next_button.first.is_enabled():
            break
        next_button.first.click()
        driver.wait(1800)
    return sorted(seen.values(), key=lambda row: (row["name"].casefold(), row["detail_url"]))


def inventory_relationship_from_classification(classification: Any) -> tuple[str, str]:
    relationship_map = {
        "already_approved": (REL_APPROVED, BUTTON_APPROVED),
        "apply_needed": (REL_APPLY_NEEDED, BUTTON_APPLY),
        "application_pending": (REL_PENDING, RELATIONSHIP_EVIDENCE_PENDING),
        "application_denied": (REL_DENIED, RELATIONSHIP_EVIDENCE_DENIED),
        "provider_capacity_blocked": (
            REL_CAPACITY_BLOCKED, RELATIONSHIP_EVIDENCE_CAPACITY_BLOCKED),
    }
    classified = relationship_map.get(str(classification or ""))
    if classified is None:
        raise MaxBountyError(
            f"maxbounty_relationship_state_unclassified:{classification}")
    return classified


def capture_program(driver: Any, summary: dict[str, str]) -> dict[str, Any]:
    if (getattr(driver, 'fresh', False) is True and getattr(driver, 'owned_session', '')
            and callable(getattr(driver, 'maxbounty_campaign_sections', None))):
        from maxbounty_fresh_catalog import capture_visible_campaign
        return capture_visible_campaign(driver, summary)
    raise MaxBountyError('maxbounty_fresh_campaign_reader_required')


def capture_historical_disposition(driver: Any, program_id: str, account_context: dict[str, Any]) -> dict[str, Any]:
    """Record a naturally observed exact account response; never call a private API."""
    if not re.fullmatch(r'[0-9]+',program_id):
        raise MaxBountyError('retirement_program_id_invalid')
    page=driver.page
    detail_url='https://affiliates.maxbounty.com/campaign/'+program_id
    api_url='https://api.maxbounty.com/affiliates/api/campaign/'+program_id
    observed={}
    def observe(response):
        if response.url==api_url and response.request.method=='GET':
            raw=response.body()
            observed.update({'url':response.url,'method':'GET','status':int(response.status),
                             'body_sha256':hashlib.sha256(raw).hexdigest()})
    page.on('response',observe)
    try:
        state,_control=_open_campaign_detail(driver,{'program_id':program_id,'detail_url':detail_url})
    finally:
        page.remove_listener('response',observe)
    retired=(observed.get('status')==404 and state.get('classification')=='terminal_provider_unavailable'
             and not page.locator('input[type=password]').count())
    payload={'schema':'glp.maxbounty.retired404.v1','program_id':program_id,
             'disposition':'RETIRED_404' if retired else 'unknown','observed_at_utc':discovery.now(),
             'account_context':account_context,'detail_url':detail_url,'response':observed,
             'classification':state.get('classification')}
    root=Path(account_context['receipts_dir']);root.mkdir(parents=True,exist_ok=True)
    path=root/(program_id+'.json')
    with path.open('x',encoding='utf-8') as handle:
        json.dump(payload,handle,sort_keys=True,indent=2)
    path.chmod(0o400)
    return {'program_id':program_id,'disposition':payload['disposition'],
            'evidence':{'path':str(path),'sha256':sha256_file(path)}}


def enumerate_search_categories(driver: Any, *, scopes=None, fetch=None,
                                previous_approved_ids=None, baseline_sources=None,
                                direct_recheck=None, max_pages=50,
                                max_campaigns=2000, evidence_dir=None, account_context=None) -> dict[str, Any]:
    """Read-only UI search/category census; custom transports remain supported."""
    if scopes is None:
        terms = ['glp','semaglutide','tirzepatide','liraglutide','retatrutide',
                 'weight','metabolic','ozempic','wegovy','zepbound','mounjaro','docsearch','32619']
        scopes = [{'key':'search-'+term,'kind':'search','value':term} for term in terms]
        scopes.append({'key':'category-health','kind':'category','value':'58'})
        scopes.append({'key':'account-approved','kind':'approved','value':''})
    if fetch is None:
        if evidence_dir is None or not hasattr(driver,'page'):
            return {'schema':discovery.SCHEMA,'status':'INCOMPLETE','complete':False,
                    'enumerated':None,'discovered_links':None,'programs':[],
                    'blockers':[{'error':'ui_evidence_directory_or_page_missing'}]}
        if not callable(getattr(driver, 'maxbounty_search_snapshot', None)):
            return {'schema': discovery.SCHEMA, 'status': 'INCOMPLETE', 'complete': False,
                    'enumerated': None, 'discovered_links': None, 'programs': [],
                    'blockers': [{'error': 'maxbounty_fresh_visible_search_reader_required'}]}
        from maxbounty_fresh_catalog import FreshVisibleListingTransport
        fetch = FreshVisibleListingTransport(driver, evidence_dir=evidence_dir)
    return discovery.collect_scopes(fetch,lambda row:capture_program(driver,row),
        scopes=scopes,previous_approved_ids=previous_approved_ids,
        baseline_sources=baseline_sources,direct_recheck=direct_recheck,
        max_pages=max_pages,max_campaigns=max_campaigns,account_context=account_context)


def enumerate_bookmarked(driver: Any, **kwargs) -> dict[str, Any]:
    """Compatibility entry name only; bookmarks cannot prove full discovery."""
    return enumerate_search_categories(driver, **kwargs)


def login_with_dom_settle(session: Any, *, wait_ms: int = 30000,
                          poll_ms: int = 500) -> dict[str, Any]:
    """Accept an existing MaxBounty session only after its dashboard DOM settles.

    The live portal was observed painting a complete authenticated dashboard after
    PortalSession's first body-text read returned empty. That produced the false
    `no_password_field_on_login_page` blocker even though no login form existed and
    the browser was already on /dashboard. This helper never retries credentials;
    it only re-reads the already-open page until the signed-in marker appears.
    """
    result = session.login()
    accepted = {"logged_in", "session_reused", "logged_in_via_sso"}
    if result.get("outcome") in accepted:
        return result
    if (result.get("reason") != "no_password_field_on_login_page"
            or "/dashboard" not in session.driver.current_url()):
        return result
    waited = 0
    while waited < wait_ms:
        state = session.page_state()
        if not state["has_password_field"] and "dashboard" in state["success_hits"]:
            return {
                **{key: value for key, value in result.items()
                   if key not in {"text_excerpt", "operator_step"}},
                "outcome": "session_reused_after_dom_settle",
                "reason": None,
                "login_submits": int(result.get("login_submits") or 0),
                "dom_settle_wait_ms": waited,
            }
        if state["has_password_field"] or state["failure_hits"]:
            break
        session.driver.wait(poll_ms)
        waited += poll_ms
    return result


def login_and_enumerate(login_url: str, *, run_id: str, headless: bool = True,
                        session_factory=None, fresh_driver=None,
                        **discovery_options) -> dict[str, Any]:
    """Consume the caller-owned vault session; never create/reuse a profile.

    MaxBounty exposes no supported publisher API for this operation. Its
    observed search/category UI transport still owns completeness and baseline
    reconciliation. A login/account read alone is not a catalogue result.
    """
    parsed_login = urlparse(str(login_url))
    if (parsed_login.scheme != 'https' or parsed_login.netloc != discovery.HOST
            or parsed_login.query or parsed_login.fragment
            or re.search(r'[\\\s%\x00-\x1f]', str(login_url))):
        raise MaxBountyError('maxbounty_sheet_login_url_invalid')
    if (session_factory is None and fresh_driver is not None
            and getattr(fresh_driver, 'read_method', '') == 'maxbounty_official_affiliate_api_v1'):
        return _enumerate_via_official_api(fresh_driver, run_id=run_id, **discovery_options)
    if (session_factory is not None or fresh_driver is None
            or getattr(fresh_driver, 'fresh', False) is not True
            or not getattr(fresh_driver, 'owned_session', '')):
        return {'schema': discovery.SCHEMA, 'status': 'BLOCKED_LOGIN',
                'login': {'outcome': 'blocked', 'reason': 'fresh_ai_browser_vault_stage_required',
                          'login_submits': 0}, 'programs': [], 'enumerated': None,
                'discovered_links': None, 'complete': False,
                'effects': {'provider_mutations': 0, 'tracking_links_minted': 0,
                            'applications_submitted': 0, 'tracking_clicks': 0}}
    fresh_driver.goto('https://affiliates.maxbounty.com/settings')
    if (fresh_driver.url != 'https://affiliates.maxbounty.com/settings'
            or [line.strip() for line in fresh_driver.body_text().splitlines()].count('ops@glpclinicscout.com') != 1):
        raise MaxBountyError('maxbounty_protected_principal_unverified')
    fresh_driver.goto('https://affiliates.maxbounty.com/dashboard')
    body = fresh_driver.body_text().lower()
    if (fresh_driver.url != 'https://affiliates.maxbounty.com/dashboard'
            or 'MaxBounty' not in fresh_driver.title()
            or not all(word in body for word in ('dashboard', 'campaigns'))):
        raise MaxBountyError('maxbounty_dashboard_identity_unverified')
    evidence_dir = discovery_options.get('evidence_dir')
    if evidence_dir is not None:
        discovery_options.setdefault('account_context', {
            'account': 'ops@glpclinicscout.com', 'portal': 'https://affiliates.maxbounty.com',
            'authenticated': True, 'run_id': run_id,
            'receipts_dir': str(Path(evidence_dir) / 'retired-404')})
    result = enumerate_search_categories(fresh_driver, **discovery_options)
    result['login'] = {'outcome': 'fresh_protected_principal_verified', 'login_submits': 0}
    result['read_method'] = 'fresh_ai_browser_vault'
    result['status'] = ('COMPLETE' if (result.get('coverage') or {}).get('schema') ==
                        'glp.maxbounty.account-catalog-coverage.v2' else 'PASS') if result.get('complete') is True else 'INCOMPLETE'
    return result


def _enumerate_via_official_api(api: Any, *, run_id: str, **discovery_options) -> dict[str, Any]:
    """Official Affiliate API v1 read (API first); same completeness oracle.

    The API session authenticates on demand and proves the principal through
    GET /affiliate before any search. Search pages, campaign detail and the
    previous-approval rechecks are the account API's own answers; nothing is
    written to the provider and no tracking link is requested or followed.
    """
    from maxbounty_publisher_api import (APIListingTransport, api_direct_recheck,
                                         capture_api_campaign)
    principal = api.authenticate()
    evidence_dir = discovery_options.pop('evidence_dir', None)
    if evidence_dir is None:
        raise MaxBountyError('maxbounty_api_evidence_directory_required')
    account_context = discovery_options.pop('account_context', None) or {
        'account': principal['account'], 'portal': 'https://affiliates.maxbounty.com',
        'authenticated': True, 'run_id': run_id,
        'receipts_dir': str(Path(evidence_dir) / 'retired-404')}
    scopes = discovery_options.pop('scopes', None)
    if scopes is None:
        terms = ['glp','semaglutide','tirzepatide','liraglutide','retatrutide',
                 'weight','metabolic','ozempic','wegovy','zepbound','mounjaro','docsearch','32619']
        scopes = [{'key':'search-'+term,'kind':'search','value':term} for term in terms]
        scopes.append({'key':'category-health','kind':'category','value':'58'})
        scopes.append({'key':'account-approved','kind':'approved','value':''})
    discovery_options.setdefault('direct_recheck', api_direct_recheck(api, account_context))
    result = discovery.collect_scopes(
        APIListingTransport(api, evidence_dir=evidence_dir),
        lambda row: capture_api_campaign(api, row), scopes=scopes,
        account_context=account_context, **discovery_options)
    result['login'] = {'outcome': 'official_api_principal_verified', 'login_submits': 0,
                       'principal': principal}
    result['read_method'] = api.read_method
    result['status'] = ('COMPLETE' if (result.get('coverage') or {}).get('schema') ==
                        'glp.maxbounty.account-catalog-coverage.v2' else 'PASS') if result.get('complete') is True else 'INCOMPLETE'
    return result


def _await_label(page: Any, patterns: Iterable[str], driver: Any = None) -> Any:
    """Return the first matching field locator, waiting for the modal to render.

    The five-question modal is drawn client-side after the apply click. A fixed
    post-click sleep read an empty modal and reported the field as missing on a form
    that renders fine (2026-08-28, campaign 28963). Polls, so a modal that is
    already up costs nothing. The application path no longer calls this helper;
    it is retained only for the disabled tracking-link builder.
    """
    waited = 0
    while True:
        for pattern in patterns:
            locator = page.get_by_label(re.compile(pattern, re.I))
            if locator.count():
                return locator
        if driver is None or waited >= FORM_FIELD_WAIT_MS:
            return None
        driver.wait(FORM_FIELD_POLL_MS)
        waited += FORM_FIELD_POLL_MS


def _fill_label(page: Any, patterns: Iterable[str], value: str,
                driver: Any = None) -> None:
    locator = _await_label(page, patterns, driver)
    if locator is None:
        raise MaxBountyError("maxbounty_form_field_not_found:" + "|".join(patterns))
    locator.first.fill(value)


def _select_label(page: Any, patterns: Iterable[str], value: str,
                  driver: Any = None) -> None:
    locator = _await_label(page, patterns, driver)
    if locator is None:
        raise MaxBountyError("maxbounty_select_not_found:" + "|".join(patterns))
    locator.first.select_option(label=value)


#: Authenticated DOM evidence showed the campaign API arriving only after more than
#: four minutes on a cold portal route. These are bounded condition waits, not fixed
#: sleeps: any observed terminal state returns immediately and total wait stays below
#: the operator's ten-minute ceiling.
CAMPAIGN_DETAIL_WAIT_MS = 360000
CAMPAIGN_DETAIL_RELOAD_AFTER_MS = 90000
CAMPAIGN_DETAIL_POLL_MS = 500
APPLICATION_FORM_WAIT_MS = 360000
APPLICATION_FORM_POLL_MS = 500
SUBMISSION_READBACK_WAIT_MS = 120000
SUBMISSION_READBACK_POLL_MS = 500
SUBMISSION_REQUEST_WAIT_MS = 10000
# The generic label helpers remain for the tracking-link builder only. The application
# modal cannot use them because its visible prose is not associated with the controls.
FORM_FIELD_WAIT_MS = 15000
FORM_FIELD_POLL_MS = 500


def _await_control_text(driver: Any, page: Any, text: str) -> Any:
    """Poll for one exact accessible-name button; used by the link builder."""
    waited = 0
    while True:
        locator = page.get_by_role("button", name=text, exact=True)
        if locator.count():
            return locator
        if waited >= CAMPAIGN_DETAIL_WAIT_MS:
            return None
        driver.wait(CAMPAIGN_DETAIL_POLL_MS)
        waited += CAMPAIGN_DETAIL_POLL_MS


def _normalized_body(page: Any) -> str:
    try:
        return " ".join(str(page.inner_text("body") or "").split())
    except Exception:  # noqa: BLE001 - state evidence must fail closed
        return ""


def _button_texts_for_evidence(page: Any) -> list[str]:
    try:
        return [" ".join(str(value or "").split()) for value in
                page.locator("button,[role=button]").all_inner_texts()][:40]
    except Exception:  # noqa: BLE001
        return []


def _campaign_api_path(program_id: str) -> str:
    return f"/affiliates/api/campaign/{program_id}"


def _campaign_relationship_projection(data: Any) -> dict[str, str]:
    """Keep only provider relationship fields needed for safe readback."""
    if not isinstance(data, dict):
        return {}
    details = data.get("details")
    if not isinstance(details, dict):
        return {}
    projection: dict[str, str] = {}
    for key in ("status", "affiliate_campaign_status"):
        value = details.get(key)
        if isinstance(value, (str, int, float, bool)):
            projection[key] = " ".join(str(value).split())[:500]
    return projection


def _pending_relationship_markers(projection: dict[str, Any]) -> list[str]:
    value = " ".join(str(projection.get("affiliate_campaign_status") or "").split())
    if value and re.search(
            r"\b(?:pending|under review|submitted|received|awaiting (?:advertiser )?approval)\b",
            value, re.I):
        return [value[:500]]
    return []


def _denied_relationship_markers(projection: dict[str, Any]) -> list[str]:
    value = " ".join(str(projection.get("affiliate_campaign_status") or "").split())
    if value and re.search(r"\b(?:denied|declined|rejected)\b", value, re.I):
        return [value[:500]]
    return []


def _provider_capacity_markers(projection: dict[str, Any]) -> list[str]:
    """Recognize MaxBounty's explicit account-wide by-request capacity stop."""
    value = " ".join(str(projection.get("affiliate_campaign_status") or "").split())
    if value and re.search(
            r"maximum number of by request campaigns.*new requests are not allowed",
            value, re.I):
        return [value[:500]]
    return []


def _observe_campaign_api_response(response: Any, program_id: str) -> dict[str, Any] | None:
    try:
        if (urlparse(str(response.url)).path != _campaign_api_path(program_id)
                or str(response.request.method).upper() != "GET"):
            return None
        raw = response.text()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        return {
            "campaign_api_status": int(response.status),
            "campaign_api_method": "GET",
            "campaign_api_body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "campaign_api_relationship": _campaign_relationship_projection(data),
        }
    except Exception:  # noqa: BLE001 - observation cannot break navigation
        return None


def _open_campaign_detail(driver: Any, program: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """Open one staged campaign and wait for a provider-grounded terminal state.

    The listener distinguishes a real provider 404 from a slow client render. One
    reload is permitted only when the route-specific API never fired; neither
    navigation is a provider mutation. Relationship controls are addressed by exact
    accessible button name, not brittle descendant text.
    """
    page = driver.page
    program_id = str(program.get("program_id") or "").strip()
    detail_url = str(program.get("detail_url") or "").strip()
    if not program_id or detail_url != f"https://affiliates.maxbounty.com/campaign/{program_id}":
        raise MaxBountyError("maxbounty_campaign_detail_identity_invalid")
    response_state: dict[str, Any] = {
        "campaign_api_status": None,
        "campaign_api_method": None,
        "campaign_api_body_sha256": "",
        "campaign_api_relationship": {},
    }

    def observe(response: Any) -> None:
        observed = _observe_campaign_api_response(response, program_id)
        if observed is not None:
            response_state.update(observed)

    listener_installed = False
    if callable(getattr(page, "on", None)):
        page.on("response", observe)
        listener_installed = True
    reloaded = False
    waited = 0
    try:
        page.goto(detail_url, wait_until="domcontentloaded")
        while True:
            apply_control = page.get_by_role("button", name=BUTTON_APPLY, exact=True)
            approved_control = page.get_by_role("button", name=BUTTON_APPROVED, exact=True)
            body = _normalized_body(page)
            folded = body.casefold()
            api_status = response_state["campaign_api_status"]
            pending_markers = _pending_relationship_markers(
                response_state["campaign_api_relationship"])
            denied_markers = _denied_relationship_markers(
                response_state["campaign_api_relationship"])
            capacity_markers = _provider_capacity_markers(
                response_state["campaign_api_relationship"])
            if api_status == 404:
                classification, control = "terminal_provider_unavailable", None
                break
            if api_status == 200 and capacity_markers:
                # The authenticated campaign API is available, but this account
                # has temporarily exhausted its by-request campaign capacity.
                # Keep the transient account-wide hold distinct from a terminal
                # campaign 404 so the serial queue stops after one observation.
                classification, control = "provider_capacity_blocked", None
                break
            if denied_markers:
                classification, control = "application_denied", None
                break
            if pending_markers:
                classification, control = "application_pending", None
                break
            if apply_control.count():
                classification, control = "apply_needed", apply_control
                break
            if approved_control.count():
                classification, control = "already_approved", approved_control
                break
            if waited >= CAMPAIGN_DETAIL_WAIT_MS:
                if api_status in {401, 403}:
                    classification = "authenticated_campaign_api_refused"
                elif api_status == 200 and "loading" not in folded:
                    classification = "control_absent_after_campaign_api_200"
                else:
                    classification = "page_not_ready"
                control = None
                break
            if (not reloaded and waited >= CAMPAIGN_DETAIL_RELOAD_AFTER_MS
                    and api_status is None):
                page.reload(wait_until="domcontentloaded")
                reloaded = True
            driver.wait(CAMPAIGN_DETAIL_POLL_MS)
            waited += CAMPAIGN_DETAIL_POLL_MS
    finally:
        if listener_installed:
            remover = getattr(page, "remove_listener", None) or getattr(page, "off", None)
            if callable(remover):
                with suppress(Exception):
                    remover("response", observe)
    state = {
        "schema": "glp.maxbounty.campaign-detail-state.v2",
        "program_id": program_id,
        "detail_url": detail_url,
        "observed_url": str(getattr(page, "url", "")),
        "classification": classification,
        "campaign_api_status": response_state["campaign_api_status"],
        "campaign_api_method": response_state["campaign_api_method"],
        "campaign_api_body_sha256": response_state["campaign_api_body_sha256"],
        "campaign_api_relationship": response_state["campaign_api_relationship"],
        "condition_waited_ms": waited,
        "condition_triggered_reload": reloaded,
        "buttons_seen": _button_texts_for_evidence(page),
        "body_chars": len(body),
        "pending_markers_seen": _pending_relationship_markers(
            response_state["campaign_api_relationship"]),
        "denied_markers_seen": _denied_relationship_markers(
            response_state["campaign_api_relationship"]),
        "provider_capacity_markers_seen": _provider_capacity_markers(
            response_state["campaign_api_relationship"]),
        "provider_mutation": False,
        "tracking_links_opened": 0,
        "tracking_clicks": 0,
    }
    return state, control


def _write_detail_state(path: Path | None, state: dict[str, Any]) -> str:
    if path is None:
        return ""
    target = Path(path).with_name(Path(path).stem + "-detail-state.json")
    return _attempt_receipt(target, state)


def _await_application_field(driver: Any, page: Any, field: str) -> Any:
    selector = APPLICATION_FIELDS[field]
    waited = 0
    while True:
        locator = page.locator(APPLICATION_DIALOG).locator(selector)
        if locator.count():
            return locator
        if waited >= APPLICATION_FORM_WAIT_MS:
            raise MaxBountyError(f"maxbounty_application_field_not_found:{field}:{selector}")
        driver.wait(APPLICATION_FORM_POLL_MS)
        waited += APPLICATION_FORM_POLL_MS


def _await_named_application_field(driver: Any, page: Any, field: str) -> Any:
    selector = NATIVE_APPLICATION_FIELDS[field]
    waited = 0
    while True:
        locator = page.locator(APPLICATION_DIALOG).locator(selector)
        if locator.count():
            return locator
        if waited >= APPLICATION_FORM_WAIT_MS:
            raise MaxBountyError(
                f"maxbounty_conditional_application_field_not_found:{field}:{selector}")
        driver.wait(APPLICATION_FORM_POLL_MS)
        waited += APPLICATION_FORM_POLL_MS


def _attached_file_projection(locator: Any) -> dict[str, Any]:
    return dict(locator.first.evaluate(
        """async el => {
          const file = el.files && el.files[0];
          if (!file) return {present:false};
          const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
          const sha256 = [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2,'0')).join('');
          return {present:true,name:file.name,size:file.size,type:file.type,sha256};
        }"""
    ) or {})


def _application_form_dom_projection(submit: Any) -> dict[str, Any]:
    """Enumerate the provider form itself instead of trusting our field list."""
    return dict(submit.first.evaluate(
        """async el => { // __glp_maxbounty_dom_projection__
          const form = el.form;
          if (!form) return {formPresent:false,controls:[],requiredControls:[]};
          const controls = [...form.elements].map((control, index) => {
            const type = (control.type || control.tagName || '').toLowerCase();
            const files = type === 'file' ? [...(control.files || [])].map(file => ({
              name:file.name,size:file.size,type:file.type,lastModified:file.lastModified,
            })) : [];
            return {
              index,
              tag:(control.tagName || '').toLowerCase(),
              type,
              name:control.name || '',
              required:!!control.required,
              disabled:!!control.disabled,
              readOnly:!!control.readOnly,
              willValidate:!!control.willValidate,
              valid:typeof control.checkValidity === 'function' ? control.checkValidity() : null,
              validationMessage:control.validationMessage || '',
              files,
            };
          });
          const fileInput = form.querySelector("input[type='file'][name='upload_creative']");
          let attachment = {present:false,files:[]};
          if (fileInput) {
            const files = [];
            for (const file of [...(fileInput.files || [])]) {
              const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
              const sha256 = [...new Uint8Array(digest)]
                .map(value => value.toString(16).padStart(2, '0')).join('');
              files.push({name:file.name,size:file.size,type:file.type,sha256});
            }
            attachment = {present:files.length > 0,files};
          }
          const requiredControls = controls.filter(control => control.required && !control.disabled);
          return {
            formPresent:true,
            formValid:form.checkValidity(),
            controls,
            controlCount:controls.length,
            requiredControls,
            requiredControlCount:requiredControls.length,
            requiredUnsatisfied:requiredControls.filter(control => control.valid !== true),
            attachment,
            submit:{
              type:el.type || '',disabled:!!el.disabled,
              ariaDisabled:el.getAttribute('aria-disabled') || '',
              formNoValidate:!!el.formNoValidate,
            },
          };
        }"""
    ) or {})


def _install_application_form_event_probe(submit: Any) -> None:
    submit.first.evaluate(
        """el => { // __glp_maxbounty_install_form_probe__
          const form = el.form;
          if (!form) throw new Error('submit_form_missing');
          window.__glpMaxBountyFormEvents = [];
          const safe = event => {
            const target = event.target || {};
            window.__glpMaxBountyFormEvents.push({
              event:event.type,name:target.name || '',
              type:target.type || target.tagName?.toLowerCase() || '',
              required:!!target.required,defaultPrevented:!!event.defaultPrevented,
              isTrusted:!!event.isTrusted,
              fileCount:target.type === 'file' ? (target.files?.length || 0) : null,
            });
          };
          for (const name of ['input','change','invalid','submit']) {
            form.addEventListener(name, safe, true);
          }
          el.addEventListener('click', safe, true);
          return true;
        }"""
    )


def _application_form_events(submit: Any) -> list[dict[str, Any]]:
    # A successful submit removes the application dialog immediately. Reading
    # through that now-absent locator makes Playwright wait for its full locator
    # timeout after the provider request has already left. Event evidence is
    # supplemental; never turn a disappeared success form into an ambiguous run.
    try:
        if not submit.count():
            return []
        return list(submit.first.evaluate(
            """el => { // __glp_maxbounty_read_form_probe__
              void el;
              return [...(window.__glpMaxBountyFormEvents || [])];
            }"""
        ) or [])
    except Exception:  # the modal may disappear between count() and evaluate()
        return []


def _application_screenshot(driver: Any, path: Path) -> dict[str, Any]:
    masked = int(driver.mask_credentials())
    driver.screenshot(path, full_page=True)
    path = Path(path)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "credential_fields_masked": masked,
    }


def _application_form_readback(page: Any, submit: Any,
                               expected: dict[str, str], image: Path) -> dict[str, Any]:
    dialog = page.locator(APPLICATION_DIALOG)
    values: dict[str, Any] = {}
    for field, selector in {**APPLICATION_FIELDS, **NATIVE_APPLICATION_FIELDS}.items():
        locator = dialog.locator(selector)
        if not locator.count():
            raise MaxBountyError(f"maxbounty_pre_submit_field_missing:{field}:{selector}")
        if field == "advertising_image":
            values[field] = _attached_file_projection(locator)
        else:
            values[field] = locator.first.input_value()
    dom_projection = _application_form_dom_projection(submit)
    form_valid = bool(dom_projection.get("formPresent") and dom_projection.get("formValid"))
    expected_values = {
        "leads_per_day": expected["leads_per_day"],
        "creative": "M",
        "traffic_type": "N",
        "native_traffic_network": expected["native_traffic_network"],
        "native_ads": expected["native_ads"],
        "custom_landing_page": expected["custom_landing_page"],
        "campaign_description": expected["campaign_description"],
    }
    mismatches = [
        field for field, wanted in expected_values.items()
        if str(values.get(field) or "") != str(wanted)
    ]
    if not _provider_valid_leads_per_day(values.get("leads_per_day")):
        mismatches.append("leads_per_day_provider_numeric_minimum")
    attached = values.get("advertising_image") or {}
    image_sha = sha256_file(image)
    if attached.get("sha256") != image_sha:
        mismatches.append("advertising_image_sha256")
    if not attached.get("present"):
        mismatches.append("advertising_image_present")
    dom_files = (dom_projection.get("attachment") or {}).get("files") or []
    if len(dom_files) != 1:
        mismatches.append("advertising_image_dom_file_count")
    else:
        if dom_files[0].get("name") != image.name:
            mismatches.append("advertising_image_dom_name")
        if int(dom_files[0].get("size") or -1) != image.stat().st_size:
            mismatches.append("advertising_image_dom_size")
        if dom_files[0].get("sha256") != image_sha:
            mismatches.append("advertising_image_dom_sha256")
    if dom_projection.get("requiredUnsatisfied"):
        mismatches.append("dom_required_controls_unsatisfied")
    if not form_valid:
        mismatches.append("form_valid")
    return {
        "verified": not mismatches,
        "mismatches": mismatches,
        "form_valid": form_valid,
        "field_values": values,
        "field_count": len(values),
        "dom_projection": dom_projection,
        "form_events": _application_form_events(submit),
    }


def _safe_provider_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("success", "message", "error", "errors", "status"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, (str, int, float, bool, type(None))):
            safe[key] = value if not isinstance(value, str) else value[:500]
        elif isinstance(value, (list, dict)):
            safe[key] = json.dumps(value, sort_keys=True, default=str)[:1000]
    return safe


def _observe_submission_response(page: Any, program_id: str) -> tuple[dict[str, Any], Any, bool]:
    state: dict[str, Any] = {"seen": False}
    target_path = f"{_campaign_api_path(program_id)}/requestApproval"

    def observe(response: Any) -> None:
        try:
            parsed = urlparse(str(response.url))
            method = str(response.request.method).upper()
            if (parsed.hostname != "api.maxbounty.com" or parsed.path != target_path
                    or method != "POST"):
                return
            raw = response.text()
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = None
            state.update({
                "seen": True,
                "method": method,
                "path": target_path,
                "http_status": int(response.status),
                "body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "projection": _safe_provider_response(data),
            })
        except Exception as exc:  # observation must not break provider mutation handling
            state.update({"seen": True, "observer_error_type": type(exc).__name__})

    installed = False
    if callable(getattr(page, "on", None)):
        page.on("response", observe)
        installed = True
    return state, observe, installed


def _remove_submission_response_observer(page: Any, observer: Any, installed: bool) -> None:
    if not installed:
        return
    remover = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    if callable(remover):
        with suppress(Exception):
            remover("response", observer)


def _observe_submission_exchange(
        page: Any, program_id: str,
        on_request: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], Any, Any, bool, bool]:
    """Observe the provider request boundary separately from its response."""
    state: dict[str, Any] = {
        "request_seen": False,
        "response_seen": False,
        "seen": False,
    }
    target_path = f"{_campaign_api_path(program_id)}/requestApproval"

    def mark_request(request: Any) -> None:
        try:
            parsed = urlparse(str(request.url))
            method = str(request.method).upper()
            if (parsed.hostname != "api.maxbounty.com" or parsed.path != target_path
                    or method != "POST"):
                return
            first = not state["request_seen"]
            state.update({
                "request_seen": True,
                "method": method,
                "path": target_path,
                "request_observed_at_utc": utc_now(),
            })
            if first and on_request is not None:
                on_request(dict(state))
        except Exception as exc:  # observation must not break provider handling
            state.update({"request_observer_error_type": type(exc).__name__})

    def mark_response(response: Any) -> None:
        try:
            parsed = urlparse(str(response.url))
            method = str(response.request.method).upper()
            if (parsed.hostname != "api.maxbounty.com" or parsed.path != target_path
                    or method != "POST"):
                return
            if not state["request_seen"]:
                mark_request(response.request)
            raw = response.text()
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = None
            state.update({
                "seen": True,
                "response_seen": True,
                "http_status": int(response.status),
                "body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "projection": _safe_provider_response(data),
            })
        except Exception as exc:  # observation must not break provider handling
            state.update({"seen": True, "response_observer_error_type": type(exc).__name__})

    request_installed = response_installed = False
    if callable(getattr(page, "on", None)):
        page.on("request", mark_request)
        page.on("response", mark_response)
        request_installed = response_installed = True
    return state, mark_request, mark_response, request_installed, response_installed


def _remove_submission_exchange_observers(
        page: Any, request_observer: Any, response_observer: Any,
        request_installed: bool, response_installed: bool) -> None:
    remover = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    if not callable(remover):
        return
    if request_installed:
        with suppress(Exception):
            remover("request", request_observer)
    if response_installed:
        with suppress(Exception):
            remover("response", response_observer)


def _await_submission_readback(driver: Any, page: Any,
                               provider_response: dict[str, Any]) -> dict[str, Any]:
    waited = 0
    while True:
        projection = provider_response.get("projection") or {}
        status = int(provider_response.get("http_status") or 0)
        if provider_response.get("seen") and 200 <= status < 300:
            if projection.get("success") is True:
                return {"verified": True, "verification_source": "provider_post_response",
                        "provider_response": dict(provider_response),
                        "condition_waited_ms": waited}
            if projection.get("success") is False:
                return {"verified": False, "verification_source": "provider_post_rejected",
                        "provider_response": dict(provider_response),
                        "condition_waited_ms": waited}
        if provider_response.get("seen") and status >= 400:
            return {"verified": False, "verification_source": "provider_post_http_error",
                    "provider_response": dict(provider_response),
                    "condition_waited_ms": waited}
        if waited >= SUBMISSION_READBACK_WAIT_MS:
            return {"verified": False, "verification_source": "provider_post_unverified",
                    "provider_response": dict(provider_response),
                    "condition_waited_ms": waited}
        driver.wait(SUBMISSION_READBACK_POLL_MS)
        waited += SUBMISSION_READBACK_POLL_MS


def _await_application_submitted_modal(
        driver: Any, page: Any, attempt_receipt_path: Path) -> dict[str, Any]:
    """Require the provider's visible post-submit modal before navigating away."""
    waited = 0
    pattern = re.compile(
        r"\bapplication\s+submitted\b"
        r"|\byou\s+have\s+successfully\s+submitted\s+your\s+request\b",
        re.I,
    )
    while True:
        dialog_texts: list[str] = []
        try:
            dialogs = page.locator("[role='dialog']:visible")
            dialog_texts = [" ".join(str(text or "").split())[:2000]
                            for text in dialogs.all_inner_texts()]
        except Exception:  # noqa: BLE001 - absence becomes a fail-closed receipt
            dialog_texts = []
        matches = [text for text in dialog_texts if pattern.search(text)]
        if matches:
            screenshot_path = Path(attempt_receipt_path).with_name(
                "application-submitted-modal.png")
            return {
                "verified": True,
                "verification_source": "visible_application_submitted_modal",
                "modal_text": matches[0][:1000],
                "condition_waited_ms": waited,
                "screenshot": _application_screenshot(driver, screenshot_path),
            }
        if waited >= SUBMISSION_READBACK_WAIT_MS:
            return {
                "verified": False,
                "verification_source": "application_submitted_modal_not_seen",
                "dialog_texts": dialog_texts,
                "condition_waited_ms": waited,
            }
        driver.wait(SUBMISSION_READBACK_POLL_MS)
        waited += SUBMISSION_READBACK_POLL_MS


def _canonical_application_readback(driver: Any, page: Any,
                                    program: dict[str, Any]) -> dict[str, Any]:
    """Re-read the canonical authenticated campaign relationship.

    MaxBounty can keep rendering ``Request approval to run`` after accepting a
    request. An exact campaign API GET reporting pending is therefore canonical;
    apply-control absence is only corroborating DOM evidence.
    """
    detail_url = str(program.get("detail_url") or "")
    program_id = str(program.get("program_id") or "")
    response_state: dict[str, Any] = {
        "campaign_api_status": None,
        "campaign_api_method": None,
        "campaign_api_body_sha256": "",
        "campaign_api_relationship": {},
    }

    def observe(response: Any) -> None:
        observed = _observe_campaign_api_response(response, program_id)
        if observed is not None:
            response_state.update(observed)

    listener_installed = False
    if callable(getattr(page, "on", None)):
        page.on("response", observe)
        listener_installed = True
    waited = 0
    last: dict[str, Any] = {}
    marker_pattern = re.compile(
        r"\b(?:application|approval|request)\b.{0,60}\b(?:pending|under review|submitted|received)\b"
        r"|\b(?:pending|under review)\b.{0,40}\b(?:application|approval|request)\b",
        re.I,
    )
    try:
        page.goto(detail_url, wait_until="domcontentloaded")
        while True:
            apply_count = page.get_by_role("button", name=BUTTON_APPLY, exact=True).count()
            approved_count = page.get_by_role("button", name=BUTTON_APPROVED, exact=True).count()
            body = _normalized_body(page)
            dom_markers = sorted({" ".join(match.group(0).split())[:160]
                                  for match in marker_pattern.finditer(body)})[:10]
            api_markers = _pending_relationship_markers(
                response_state["campaign_api_relationship"])
            combined_markers = ([BUTTON_APPROVED] if approved_count else []) + api_markers + dom_markers
            dom_pending_apply_absent = bool(apply_count == 0 and dom_markers)
            verified = bool(approved_count or api_markers or dom_pending_apply_absent)
            verification_authority = (
                "campaign_api_pending" if api_markers else
                ("approved_control" if approved_count else
                 ("dom_pending_apply_absent" if dom_pending_apply_absent else "none"))
            )
            last = {
                "verified": verified,
                "verification_authority": verification_authority,
                "relationship_control_after_present": bool(apply_count),
                "relationship_control_after": BUTTON_APPLY if apply_count else "",
                "approved_control_present": bool(approved_count),
                "pending_or_approved_markers": combined_markers,
                "campaign_api_status": response_state["campaign_api_status"],
                "campaign_api_method": response_state["campaign_api_method"],
                "campaign_api_body_sha256": response_state["campaign_api_body_sha256"],
                "campaign_api_relationship": response_state["campaign_api_relationship"],
                "buttons_seen": _button_texts_for_evidence(page),
                "body_chars": len(body),
                "observed_url": str(getattr(page, "url", "")),
                "condition_waited_ms": waited,
            }
            if last["verified"] or waited >= SUBMISSION_READBACK_WAIT_MS:
                return last
            driver.wait(SUBMISSION_READBACK_POLL_MS)
            waited += SUBMISSION_READBACK_POLL_MS
    finally:
        if listener_installed:
            remover = getattr(page, "remove_listener", None) or getattr(page, "off", None)
            if callable(remover):
                with suppress(Exception):
                    remover("response", observe)


def submit_application(driver: Any, program: dict[str, Any], *,
                       policy_path: Path = DEFAULT_GATE_POLICY,
                       approved_image: Path | None = None,
                       pre_submit_check: Callable[[], None] | None = None,
                       attempt_receipt_path: Path | None = None) -> dict[str, Any]:
    """Drive every base and Native-conditional field, then prove canonical state."""
    if approved_image is None:
        require_mutation_gate(policy_path)
        policy = load_policy(policy_path)
        image = _repo_file((policy.get("ad_placement_image") or {}).get("path"))
    else:
        if pre_submit_check is None:
            raise GateClosed("maxbounty_external_gate_check_missing")
        pre_submit_check()
        image = Path(approved_image).resolve()
    if attempt_receipt_path is None:
        raise GateClosed("maxbounty_provider_attempt_receipt_required")
    if image is None or not image.is_file():
        raise GateClosed("maxbounty_operator_approved_image_not_registered")
    page = driver.page
    state, button = _open_campaign_detail(driver, program)
    detail_state_sha = _write_detail_state(attempt_receipt_path, state)
    detail_state_path = (Path(attempt_receipt_path).with_name(
        Path(attempt_receipt_path).stem + "-detail-state.json")
        if attempt_receipt_path is not None else None)
    terminal_status = {
        "terminal_provider_unavailable": STATUS_TERMINAL_UNAVAILABLE,
        "already_approved": STATUS_TERMINAL_ALREADY_APPROVED,
        "application_pending": STATUS_PENDING_NO_ATTEMPT,
        "provider_capacity_blocked": STATUS_PROVIDER_CAPACITY_BLOCKED,
    }.get(state["classification"])
    if terminal_status:
        observed_at = utc_now()
        attempt_sha = _attempt_receipt(attempt_receipt_path, {
            "schema": "glp.maxbounty.provider-application-attempt.v1",
            "status": terminal_status,
            "program_id": str(program.get("program_id") or ""),
            "program_name": str(program.get("name") or ""),
            "detail_url": str(program.get("detail_url") or ""),
            "observed_at_utc": observed_at,
            "provider_application_attempts": 0,
            "provider_mutations": 0,
            "tracking_links_opened": 0,
            "tracking_clicks": 0,
            "campaign_detail_state": state,
            "campaign_detail_state_path": str(detail_state_path or ""),
            "campaign_detail_state_sha256": detail_state_sha,
        })
        return {
            "status": terminal_status,
            "program_id": program.get("program_id"),
            "observed_at_utc": observed_at,
            "provider_mutations": 0,
            "provider_application_attempts": 0,
            "tracking_links_opened": 0,
            "tracking_clicks": 0,
            "attempt_receipt": str(attempt_receipt_path or ""),
            "attempt_receipt_sha256": attempt_sha,
            "campaign_detail_state": state,
        }
    if button is None or not button.count():
        _attempt_receipt(attempt_receipt_path, {
            "schema": "glp.maxbounty.provider-application-attempt.v1",
            "status": "BLOCKED_PRE_ATTEMPT",
            "program_id": str(program.get("program_id") or ""),
            "program_name": str(program.get("name") or ""),
            "detail_url": str(program.get("detail_url") or ""),
            "blocked_at_utc": utc_now(),
            "provider_application_attempts": 0,
            "provider_mutations": 0,
            "tracking_links_opened": 0,
            "tracking_clicks": 0,
            "campaign_detail_state": state,
        })
        raise MaxBountyError(
            "maxbounty_campaign_detail_not_applyable:%s" % state["classification"])
    button.first.click()
    dialog = page.locator(APPLICATION_DIALOG)
    _await_application_field(driver, page, "leads_per_day")
    submit = dialog.get_by_role("button", name="Submit", exact=True)
    if not submit.count():
        raise MaxBountyError("maxbounty_application_submit_not_found")
    _install_application_form_event_probe(submit)
    draft = application_draft(program)
    _await_application_field(driver, page, "leads_per_day").first.fill(
        draft["leads_per_day"])
    _await_application_field(driver, page, "creative").first.select_option(
        label=draft["creative"])
    _await_application_field(driver, page, "traffic_type").first.select_option(
        label=draft["traffic_type"])
    for field in NATIVE_APPLICATION_FIELDS:
        _await_named_application_field(driver, page, field).first.fill(draft[field])
    _await_application_field(driver, page, "campaign_description").first.fill(
        draft["campaign_description"])
    screenshot_root = Path(attempt_receipt_path).parent
    stage_screenshots = {
        "form_filled_before_image": _application_screenshot(
            driver, screenshot_root / "application-form-filled-before-image.png"),
    }
    _await_application_field(driver, page, "advertising_image").first.set_input_files(
        str(image))
    # A human naturally pauses while the browser publishes the File object into
    # the controlled React form. The old lane clicked immediately after
    # set_input_files(), which can race that state even though the DOM FileList is
    # already populated. Wait once, then prove the browser-owned state.
    driver.wait(1000)
    stage_screenshots["image_attached"] = _application_screenshot(
        driver, screenshot_root / "application-image-attached.png")
    # The exact applyable state was established before opening the modal. Do not
    # scan the whole modal/page body for generic words such as "pending" here.
    pre_submit_pending_markers: list[str] = []
    image_sha = sha256_file(image)
    form_readback = _application_form_readback(page, submit, draft, image)
    pre_submit_path = Path(attempt_receipt_path).with_name(
        f"pre-submit-{str(program.get('program_id') or '').strip()}.json")
    identity_config_sha = (
        sha256_file(APPLICATION_IDENTITY_CONFIG)
        if APPLICATION_IDENTITY_CONFIG.is_file() else ""
    )
    coupon_section_present = bool(str(program.get("coupon_section") or "").strip())
    coupons = program.get("coupons") if isinstance(program.get("coupons"), list) else []
    pre_submit_sha = _attempt_receipt(pre_submit_path, {
        "schema": "glp.maxbounty.application-pre-submit.v1",
        "status": ("PREPARED_VERIFIED_NOT_SUBMITTED" if form_readback["verified"]
                   else "BLOCKED_PRE_SUBMIT_VERIFICATION_FAILED"),
        "prepared_at_utc": utc_now(),
        "program_id": str(program.get("program_id") or ""),
        "program_name": str(program.get("name") or ""),
        "detail_url": str(program.get("detail_url") or ""),
        "business_identity": {
            "legal_name": CANONICAL_BUSINESS_IDENTITY,
            "configuration_path": str(APPLICATION_IDENTITY_CONFIG),
            "configuration_sha256": identity_config_sha,
            "sensitive_identity_values_recorded": False,
        },
        "fields": {
            "leads_per_day": {"selector": APPLICATION_FIELDS["leads_per_day"],
                              "value": draft["leads_per_day"]},
            "creative": {"selector": APPLICATION_FIELDS["creative"],
                         "value": draft["creative"], "provider_value": "M"},
            "advertising_image": {
                "selector": APPLICATION_FIELDS["advertising_image"],
                "value": str(image), "sha256": image_sha,
                "browser_attachment": form_readback["field_values"].get(
                    "advertising_image", {}),
            },
            "traffic_type": {"selector": APPLICATION_FIELDS["traffic_type"],
                             "value": draft["traffic_type"], "provider_value": "N"},
            "campaign_description": {
                "selector": APPLICATION_FIELDS["campaign_description"],
                "value": draft["campaign_description"],
            },
            "native_traffic_network": {
                "selector": NATIVE_APPLICATION_FIELDS["native_traffic_network"],
                "value": draft["native_traffic_network"],
            },
            "native_ads": {"selector": NATIVE_APPLICATION_FIELDS["native_ads"],
                           "value": draft["native_ads"]},
            "custom_landing_page": {
                "selector": NATIVE_APPLICATION_FIELDS["custom_landing_page"],
                "value": draft["custom_landing_page"],
            },
        },
        "coupon_capture": {
            "status": ("captured_from_campaign" if coupon_section_present or coupons
                       else "captured_no_coupon_exposed"),
            "coupon_section_present": coupon_section_present,
            "coupons": coupons,
        },
        "stage_screenshots": stage_screenshots,
        "form_readback": form_readback,
        "provider_application_attempts": 0,
        "provider_mutations": 0,
        "tracking_links_opened": 0,
        "tracking_clicks": 0,
    })
    if not form_readback["verified"]:
        raise MaxBountyError(
            "maxbounty_pre_submit_verification_failed:"
            + ",".join(form_readback["mismatches"]))
    if pre_submit_check is None:
        require_mutation_gate(policy_path)
    else:
        pre_submit_check()
    reserved_at = utc_now()
    attempt_id = hashlib.sha256(
        f"{Path(attempt_receipt_path or '').resolve()}|{program.get('program_id')}|"
        f"{reserved_at}|{os.getpid()}".encode("utf-8")
    ).hexdigest()
    reserved_event = {
        "state": "SUBMIT_CONTROL_RESERVED_NO_PROVIDER_REQUEST",
        "at_utc": reserved_at,
    }
    base_attempt = {
        "schema": "glp.maxbounty.provider-application-attempt.v1",
        "attempt_id": attempt_id,
        "program_id": str(program.get("program_id") or ""),
        "program_name": str(program.get("name") or ""),
        "detail_url": str(program.get("detail_url") or ""),
        "relationship_before": REL_APPLY_NEEDED,
        "relationship_control_before": BUTTON_APPLY,
        "ad_image_sha256": image_sha,
        "application_field_selectors": {
            **APPLICATION_FIELDS, **NATIVE_APPLICATION_FIELDS},
        "pre_submit_receipt_path": str(pre_submit_path),
        "pre_submit_receipt_sha256": pre_submit_sha,
        "campaign_detail_state_path": str(detail_state_path or ""),
        "campaign_detail_state_sha256": detail_state_sha,
        "pre_submit_pending_markers": pre_submit_pending_markers,
        "reserved_at_utc": reserved_at,
        "gate_slot_consumed": 0,
        "provider_request_observed": False,
        "stage_screenshots": stage_screenshots,
        "tracking_links_opened": 0,
        "tracking_clicks": 0,
    }

    request_journal: list[dict[str, Any]] = []

    def journal_provider_request(observed: dict[str, Any]) -> None:
        event = {"state": "PROVIDER_REQUEST_OBSERVED", "at_utc": utc_now()}
        request_journal[:] = [event]
        _attempt_receipt(attempt_receipt_path, {
            **base_attempt,
            "status": "PROVIDER_REQUEST_OBSERVED_AWAITING_RESPONSE",
            "attempted_at_utc": event["at_utc"],
            "gate_slot_consumed": 1,
            "provider_request_observed": True,
            "provider_application_attempts": 1,
            "journal_events": [reserved_event, event],
            "provider_response": observed,
        })

    (provider_response, request_observer, response_observer,
     request_observer_installed, response_observer_installed) = (
        _observe_submission_exchange(
            page, str(program.get("program_id") or ""), journal_provider_request))
    _attempt_receipt(attempt_receipt_path, {
        **base_attempt,
        "status": "SUBMIT_CONTROL_RESERVED_NO_PROVIDER_REQUEST",
        "provider_application_attempts": 0,
        "journal_events": [reserved_event],
    })
    try:
        try:
            submit.first.click()
        except Exception as exc:
            attempted_at = utc_now()
            request_seen = bool(provider_response.get("request_seen"))
            _attempt_receipt(attempt_receipt_path, {
                **base_attempt,
                "status": ("PROVIDER_ATTEMPT_AMBIGUOUS" if request_seen
                           else "SUBMIT_CONTROL_CLICK_FAILED_NO_PROVIDER_REQUEST"),
                "attempted_at_utc": attempted_at,
                "gate_slot_consumed": 1 if request_seen else 0,
                "provider_request_observed": request_seen,
                "provider_application_attempts": 1 if request_seen else 0,
                "journal_events": [reserved_event, {
                    "state": "CLICK_DISPATCH_AMBIGUOUS", "at_utc": attempted_at
                }] + request_journal,
                "provider_response": dict(provider_response),
                "form_events_at_submit": _application_form_events(submit),
                "error": f"{type(exc).__name__}:{str(exc)[:300]}",
            })
            raise
        attempted_at = utc_now()
        click_event = {"state": "SUBMIT_CONTROL_CLICKED", "at_utc": attempted_at}
        waited_for_request_ms = 0
        while (not provider_response.get("request_seen")
               and waited_for_request_ms < SUBMISSION_REQUEST_WAIT_MS):
            driver.wait(SUBMISSION_READBACK_POLL_MS)
            waited_for_request_ms += SUBMISSION_READBACK_POLL_MS
        if not provider_response.get("request_seen"):
            no_request_screenshot = _application_screenshot(
                driver, Path(attempt_receipt_path).with_name(
                    "submit-control-clicked-no-provider-request.png"))
            _attempt_receipt(attempt_receipt_path, {
                **base_attempt,
                "status": "SUBMIT_CONTROL_CLICKED_NO_PROVIDER_REQUEST",
                "attempted_at_utc": attempted_at,
                "reconciled_at_utc": utc_now(),
                "provider_application_attempts": 0,
                "gate_slot_consumed": 0,
                "provider_request_observed": False,
                "journal_events": [reserved_event, click_event],
                "provider_response": dict(provider_response),
                "form_events_at_submit": _application_form_events(submit),
                "request_waited_ms": waited_for_request_ms,
                "no_request_screenshot": no_request_screenshot,
            })
            raise MaxBountyError(
                "maxbounty_submit_control_emitted_no_provider_request")
        request_event = (request_journal[0] if request_journal else {
            "state": "PROVIDER_REQUEST_OBSERVED",
            "at_utc": str(provider_response.get("request_observed_at_utc") or utc_now()),
        })
        dispatched_events = [reserved_event, click_event, request_event]
        _attempt_receipt(attempt_receipt_path, {
            **base_attempt,
            "status": "PROVIDER_ATTEMPTED_AWAITING_READBACK",
            "attempted_at_utc": attempted_at,
            "gate_slot_consumed": 1,
            "provider_request_observed": True,
            "provider_application_attempts": 1,
            "journal_events": dispatched_events,
            "provider_response": dict(provider_response),
            "form_events_at_submit": _application_form_events(submit),
            "request_waited_ms": waited_for_request_ms,
        })
        modal_readback = _await_application_submitted_modal(
            driver, page, Path(attempt_receipt_path))
        readback = _await_submission_readback(driver, page, provider_response)
        canonical_readback = _canonical_application_readback(driver, page, program)
        explicit_rejection = readback["verification_source"] in {
            "provider_post_rejected", "provider_post_http_error"}
        if (explicit_rejection or not modal_readback["verified"]
                or not canonical_readback["verified"]):
            _attempt_receipt(attempt_receipt_path, {
                **base_attempt,
                "status": "PROVIDER_ATTEMPT_AMBIGUOUS",
                "attempted_at_utc": attempted_at,
                "reconciled_at_utc": utc_now(),
                "gate_slot_consumed": 1,
                "provider_request_observed": True,
                "provider_application_attempts": 1,
                "journal_events": dispatched_events + [{
                    "state": "READBACK_AMBIGUOUS", "at_utc": utc_now()}],
                "readback": (
                    "provider explicitly rejected submission" if explicit_rejection
                    else ("application submitted modal not observed"
                          if not modal_readback["verified"]
                          else "canonical detail page did not prove apply control gone and pending/approved")),
                "readback_evidence": readback,
                "post_submit_modal_evidence": modal_readback,
                "canonical_readback_evidence": canonical_readback,
            })
            raise MaxBountyError("maxbounty_application_submission_not_verified")
        submitted_at = utc_now()
        attempt_sha = _attempt_receipt(attempt_receipt_path, {
            **base_attempt,
            "status": STATUS_SUBMITTED,
            "attempted_at_utc": attempted_at,
            "submitted_at_utc": submitted_at,
            "reconciled_at_utc": submitted_at,
            "gate_slot_consumed": 1,
            "provider_request_observed": True,
            "provider_application_attempts": 1,
            "provider_mutations": 1,
            "journal_events": dispatched_events + [{
                "state": STATUS_SUBMITTED, "at_utc": submitted_at}],
            "relationship_after": (
                "approved" if canonical_readback["approved_control_present"] else
                "application_pending"),
            "readback": (
                "canonical campaign detail has no apply control and shows pending/approved state"),
            "readback_evidence": readback,
            "post_submit_modal_evidence": modal_readback,
            "canonical_readback_evidence": canonical_readback,
        })
    finally:
        _remove_submission_exchange_observers(
            page, request_observer, response_observer,
            request_observer_installed, response_observer_installed)
    return {"status": STATUS_SUBMITTED, "program_id": program.get("program_id"),
            "submitted_at_utc": submitted_at, "answers": draft,
            "ad_image_sha256": image_sha, "provider_mutations": 1,
            "provider_application_attempts": 1,
            "gate_slot_consumed": 1, "attempt_id": attempt_id,
            "pre_submit_receipt": str(pre_submit_path),
            "pre_submit_receipt_sha256": pre_submit_sha,
            "post_submit_modal_evidence": modal_readback,
            "attempt_receipt": str(attempt_receipt_path or ""),
            "attempt_receipt_sha256": attempt_sha}


def _bind_rail_tracking_capture(
    program: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Apply the rail table's capture contract before any caller can persist it."""
    from glp_affiliate_link_rails import bind_tracking_capture  # noqa: PLC0415

    return bind_tracking_capture(
        "MaxBounty",
        str(program.get("program_id") or ""),
        str(result.get("tracking_link") or ""),
        program=str(program.get("name") or ""),
        official_url=str(program.get("official_url") or ""),
        base=result,
    )


def mint_tracking_link(driver: Any, program: dict[str, Any], configuration: dict[str, Any], *,
                       policy_path: Path = DEFAULT_GATE_POLICY,
                       retrieval_authorization_path: Path | None = None) -> dict[str, Any]:
    """Drive the four-option builder and read Copy link. Never clicks the link."""
    program_id = str(program.get("program_id") or "").strip()

    def require_authority() -> dict[str, Any]:
        if retrieval_authorization_path is not None:
            return require_tracking_retrieval_authorization(
                Path(retrieval_authorization_path), program_id)
        return {"status": "OPEN_CANONICAL_GATE", **require_mutation_gate(policy_path)}

    authority = require_authority()
    if configuration.get("traffic_type") != TRAFFIC_TYPE:
        raise MaxBountyError("maxbounty_traffic_type_must_be_native")
    page = driver.page
    page.goto(program["detail_url"], wait_until="domcontentloaded")
    # Same client-side render race as submit_application: the builder control is not on
    # the page when domcontentloaded fires. Poll for it instead of reading too early.
    button = _await_control_text(driver, page, BUTTON_APPROVED)
    if button is None or not button.count():
        raise MaxBountyError("maxbounty_build_tracking_link_button_not_present")
    button.first.click()
    driver.wait(1000)
    # Current approved-campaign UI (observed on 27300, 2026-08-30) exposes the
    # already-issued provider href directly in #trackingLink as soon as the
    # builder expands. No traffic or creative radio is selected and there is no
    # generate button in this state. Capture that field before attempting the
    # legacy configurable builder path: this is a retrieval, not a mutation.
    existing_link = ""
    existing = None
    waited_existing = 0

    def href_from_tracking_code(raw: str) -> str:
        raw = str(raw or "").strip()
        anchor = re.search(r"href\s*=\s*['\"](https://[^'\"]+)['\"]", raw, re.I)
        bare = re.search(r"https://[^\s<>\"']+", raw, re.I)
        return html.unescape(
            (anchor.group(1) if anchor else (bare.group(0) if bare else "")).strip()
        )

    while waited_existing <= FORM_FIELD_WAIT_MS:
        existing = page.locator("#trackingLink")
        if existing.count():
            existing_raw = existing.first.input_value().strip()
            existing_link = href_from_tracking_code(existing_raw)
            if existing_link:
                break
        driver.wait(FORM_FIELD_POLL_MS)
        waited_existing += FORM_FIELD_POLL_MS
    if existing is not None and existing.count():
        parsed_existing = urlparse(existing_link) if existing_link else None
        if (parsed_existing and parsed_existing.scheme == "https"
                and parsed_existing.hostname and not parsed_existing.username):
            authority = require_authority()
            observed_configuration = {
                **configuration,
                "capture_mode": "existing_copy_link_textarea",
                "ui_traffic_selection": "none",
                "ui_creative_selection": "none",
                "generated_in_this_run": False,
            }
            result = {
                "status": "MINTED_NOT_CLICKED",
                "retrieval_status": "CAPTURED_EXISTING_NOT_CLICKED",
                "program_id": program.get("program_id"),
                "tracking_link": existing_link,
                "configuration": observed_configuration,
                "minted_at_utc": utc_now(),
                "tracking_clicks": 0,
                "provider_mutations": 0,
            }
            if retrieval_authorization_path is not None:
                result.update({
                    "retrieval_authorization_path": authority["authorization_path"],
                    "retrieval_authorization_sha256": authority["authorization_sha256"],
                    "authorization_mode": "operator_scoped_tracking_retrieval",
                })
            return _bind_rail_tracking_capture(program, result)
    # Current UI radio path. Selecting these two controls is the observed
    # Native / own-creative configuration; the page then writes the provider
    # href into #trackingLink. No tracking navigation is performed.
    traffic_radio = page.locator("#traffic-Native")
    creative_radio = page.locator("#creative-raw")
    if traffic_radio.count() and creative_radio.count():
        require_authority()
        traffic_radio.first.check(force=True)
        # Selecting traffic re-renders the creative controls. The original
        # locator can remain attached long enough for Playwright to attempt a
        # check, then detach or report that the radio never changed state.
        # Let the dependent control settle, reacquire it, and fall back to its
        # provider-owned label without navigating to the generated URL.
        driver.wait(FORM_FIELD_POLL_MS * 2)
        creative_radio = page.locator("#creative-raw")
        try:
            creative_radio.first.check(force=True)
        except Exception as check_error:
            driver.wait(FORM_FIELD_POLL_MS * 2)
            creative_radio = page.locator("#creative-raw")
            if not creative_radio.count():
                raise check_error
            try:
                creative_radio.first.click(force=True)
            except Exception:
                creative_label = page.locator("label[for='creative-raw']")
                if not creative_label.count():
                    raise check_error
                creative_label.first.click(force=True)
        driver.wait(FORM_FIELD_POLL_MS * 2)
        creative_radio = page.locator("#creative-raw")
        is_checked = getattr(creative_radio.first, "is_checked", None)
        if callable(is_checked) and not is_checked():
            creative_label = page.locator("label[for='creative-raw']")
            if creative_label.count():
                creative_label.first.click(force=True)
                driver.wait(FORM_FIELD_POLL_MS * 2)
                creative_radio = page.locator("#creative-raw")
                is_checked = getattr(creative_radio.first, "is_checked", None)
            if callable(is_checked) and not is_checked():
                # Bootstrap's custom radio can absorb both forced input and
                # label clicks while a dependent panel is re-rendering. Use
                # the native checked setter and emit only local input/change
                # events; this does not navigate to or open the tracking href.
                creative_radio.first.evaluate(
                    """element => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'checked'
                        ).set;
                        setter.call(element, true);
                        element.dispatchEvent(new Event('input', {bubbles: true}));
                        element.dispatchEvent(new Event('change', {bubbles: true}));
                    }"""
                )
                driver.wait(FORM_FIELD_POLL_MS * 2)
                creative_radio = page.locator("#creative-raw")
                is_checked = getattr(creative_radio.first, "is_checked", None)
            if callable(is_checked) and not is_checked():
                raise RuntimeError("maxbounty_creative_radio_not_selected_after_retry")
        generated_link = ""
        waited_generated = 0
        while waited_generated <= FORM_FIELD_WAIT_MS:
            raw = existing.first.input_value().strip() if existing and existing.count() else ""
            generated_link = href_from_tracking_code(raw)
            if generated_link:
                break
            driver.wait(FORM_FIELD_POLL_MS)
            waited_generated += FORM_FIELD_POLL_MS
        parsed_generated = urlparse(generated_link) if generated_link else None
        if (parsed_generated and parsed_generated.scheme == "https"
                and parsed_generated.hostname and not parsed_generated.username):
            authority = require_authority()
            generated_configuration = {
                **configuration,
                "capture_mode": "native_raw_radio_tracking_textarea",
                "ui_traffic_selection": "Native",
                "ui_creative_selection": "raw",
                "ui_landing_page_selection": "campaign_default_only",
                "generated_in_this_run": True,
            }
            result = {
                "status": "MINTED_NOT_CLICKED",
                "retrieval_status": "GENERATED_BY_CONFIG_NOT_CLICKED",
                "program_id": program.get("program_id"),
                "tracking_link": generated_link,
                "configuration": generated_configuration,
                "minted_at_utc": utc_now(),
                "tracking_clicks": 0,
                "provider_mutations": 1,
            }
            if retrieval_authorization_path is not None:
                result.update({
                    "retrieval_authorization_path": authority["authorization_path"],
                    "retrieval_authorization_sha256": authority["authorization_sha256"],
                    "authorization_mode": "operator_scoped_tracking_retrieval",
                })
            return _bind_rail_tracking_capture(program, result)
    _select_label(page, (r"traffic type", r"traffic"), TRAFFIC_TYPE, driver)
    _select_label(page, (r"creative",), str(configuration["creative"]), driver)
    _select_label(page, (r"landing page", r"destination"),
                  str(configuration["landing_page"]), driver)
    for number in range(1, 5):
        value = str((configuration.get("sub_ids") or {}).get(f"sub_id_{number}") or "")
        if value:
            _fill_label(page, (rf"sub\s*id\s*{number}", rf"sub{number}"), value, driver)
    authority = require_authority()
    generate = page.get_by_role("button", name=re.compile(r"^(?:build|generate|create)(?: tracking)? link$", re.I))
    if not generate.count():
        raise MaxBountyError("maxbounty_tracking_generate_control_not_found")
    generate.first.click()
    driver.wait(1800)
    popup = page.get_by_text("Copy link", exact=True)
    inputs = page.locator("input[value^='http'], textarea")
    link = ""
    for index in range(inputs.count()):
        candidate = inputs.nth(index).input_value().strip()
        if candidate.startswith("https://"):
            link = candidate
            break
    if not popup.count() or not link:
        raise MaxBountyError("maxbounty_copy_link_popup_not_verified")
    result = {"status": "MINTED_NOT_CLICKED", "program_id": program.get("program_id"),
            "tracking_link": link, "configuration": configuration,
            "minted_at_utc": utc_now(), "tracking_clicks": 0, "provider_mutations": 1}
    if retrieval_authorization_path is not None:
        result.update({
            "retrieval_authorization_path": authority["authorization_path"],
            "retrieval_authorization_sha256": authority["authorization_sha256"],
            "authorization_mode": "operator_scoped_tracking_retrieval",
        })
    return _bind_rail_tracking_capture(program, result)


__all__ = [
    "TRAFFIC_TYPE", "CREATIVE_ANSWER", "LEADS_PER_DAY", "BUTTON_APPROVED", "BUTTON_APPLY",
    "GateClosed", "MaxBountyError", "load_policy", "mutation_gate_status",
    "require_mutation_gate", "require_tracking_retrieval_authorization", "rollout_status",
    "classify_button", "classify_vertical",
    "payout_details", "parse_attribution_days", "parse_coupons", "campaign_description",
    "application_draft", "sub_ids", "tracking_configuration", "enumerate_bookmarked",
    "login_and_enumerate", "submit_application", "mint_tracking_link",
]
