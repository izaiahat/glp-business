#!/usr/bin/env python3
"""Affiliati Network adapter for the documented CAKE Affiliate API.

The provider's Swagger document proves this is not a read-only API:

* GET /Offers/Feed enumerates the complete offer catalogue, including offer
  status, contract id, payout, restrictions, media types and landing page;
* GET /Offers/Campaign and GET /Offers/Event expose joined campaign and paid
  event detail;
* GET /Creatives/GetCode returns creative/tracking code for a joined campaign;
* POST /Offers/Campaign/Create submits an offer application.

Application is nevertheless fail-closed here. ``apply`` requires an explicit
operator batch-authority flag and terms acceptance; catalogue and reconciliation
runs can never submit merely because the endpoint exists.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from . import _base as b

NETWORK = "affiliati"
DISPLAY = "Affiliati"
API = "https://login.affiliatinetwork.com/affiliates/api"
PORTAL = "https://login.affiliatinetwork.com/affiliates/"
DOCS = [
    "https://login.affiliatinetwork.com/affiliates/api/docs",
    "local: reports/glp-clinic-scout/affiliate-machine/AFFILIATI-ONBOARD-20260826/"
    "source/cake-affiliate-api-schema.json",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "api", "apply": "api"}
SEO_MEDIA_TYPE_ID = 5
ROW_LIMIT = 100
RELEVANT_OFFER_RE = re.compile(
    r"\b(?:glp[- ]?1|semaglutide|tirzepatide|weight\s*loss|telehealth|telemedicine|obesity)\b",
    re.I,
)


def _credentials() -> tuple[str, str]:
    secret = b.secret_json("affiliati", "api.json")
    affiliate_id = str(secret.get("affiliate_id") or "").strip()
    api_key = str(secret.get("api_key") or "").strip()
    if not affiliate_id or not api_key:
        raise RuntimeError("affiliati_api_credentials_incomplete")
    return affiliate_id, api_key


def _request(transport, method: str, path: str, *, params: dict | None = None,
             json_body: dict | None = None):
    affiliate_id, api_key = _credentials()
    query = {"affiliate_id": affiliate_id, "api_key": api_key}
    query.update(params or {})
    return transport.request(method, f"{API}{path}", params=query, json_body=json_body)


def _relationship(row: dict[str, Any]) -> str:
    status = row.get("offer_status") if isinstance(row.get("offer_status"), dict) else {}
    raw = str(status.get("offer_status_name") or row.get("offer_status_name") or "").strip().casefold()
    campaign_id = str(row.get("campaign_id") or "").strip()
    if "pending" in raw:
        return "pending"
    if campaign_id and raw in {"active", "public", "approved"}:
        return "approved"
    if raw in {"rejected", "declined", "denied"}:
        return "rejected"
    # Offer status describes catalogue availability, not an affiliate relationship.
    # Without a campaign id, even a public/active offer is not claimed as joined.
    if not campaign_id or raw in {"apply to run", "available"}:
        return "notjoined"
    return raw or "unknown"


def _paid_event(description: object) -> str:
    # CAKE puts the conversion event on the same physical line as
    # "Commission Upon". Preserve that line boundary: flattening the description
    # first caused the following "Best Traffic Source" prose to become part of
    # TrimRX's paid event, making cross-rail comparisons dishonest.
    text = str(description or "").replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(
        r"(?:Commission\s+(?:upon|on)\s*[-:]?\s*)([^\n]+)",
        text, re.I,
    )
    if match:
        return match.group(1).strip(" .")
    converts = re.search(r"\bConverts?\s+on\s+([^\n]+)", text, re.I)
    return converts.group(1).strip(" .") if converts else ""


def _payout_display(row: dict[str, Any]) -> str:
    model = str(row.get("price_format") or "").strip()
    symbol = str(row.get("currency_symbol") or "$" if model.casefold() != "revshare" else "").strip()
    if model.casefold() == "revshare":
        value = row.get("percentage_max")
        if value is None:
            value = row.get("price")
        return f"{value:g}% RevShare" if isinstance(value, (int, float)) else f"{value}% RevShare"
    value = row.get("price")
    if isinstance(value, (int, float)):
        return f"{symbol}{value:g} {model}".strip()
    return f"{symbol}{value or ''} {model}".strip()


def normalize_offer(row: dict[str, Any]) -> dict[str, Any]:
    media = row.get("allowed_media_types") if isinstance(row.get("allowed_media_types"), list) else []
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    restrictions = str(row.get("restrictions") or "")
    has_seo = any(int(item.get("media_type_id") or 0) == SEO_MEDIA_TYPE_ID for item in media)
    tagged_seo = any("seo allowed" in str(item.get("tag_name") or "").casefold() for item in tags)
    no_seo = bool(re.search(r"\bno\s+seo\b", restrictions, re.I))
    status = row.get("offer_status") if isinstance(row.get("offer_status"), dict) else {}
    model = str(row.get("price_format") or "").strip()
    headline = row.get("percentage_max") if model.casefold() == "revshare" else row.get("price")
    return {
        "program_id": str(row.get("offer_id") or ""),
        "offer_contract_id": str(row.get("offer_contract_id") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "name": str(row.get("offer_name") or "").strip(),
        "relationship": _relationship(row),
        "offer_status_raw": str(status.get("offer_status_name") or ""),
        "vertical": str(row.get("vertical_name") or ""),
        "url": str(row.get("preview_link") or ""),
        "official_url": str(row.get("preview_link") or ""),
        "commission": _payout_display(row),
        "headline_payout": headline,
        "payout_model": model,
        "payout_display": _payout_display(row),
        "paid_event": _paid_event(row.get("description")),
        "paid_event_basis": "authenticated_affiliati_offer_feed_description",
        "currency": "USD" if int(row.get("currency_id") or 1) == 1 else "",
        "price_min": row.get("price_min"),
        "price_max": row.get("price_max"),
        "percentage_min": row.get("percentage_min"),
        "percentage_max": row.get("percentage_max"),
        "seo_allowed": bool((has_seo or tagged_seo) and not no_seo),
        "allowed_media_types": [str(item.get("media_type_name") or "") for item in media],
        "description": str(row.get("description") or ""),
        "restrictions": restrictions,
        "advertiser_extended_terms": str(row.get("advertiser_extended_terms") or ""),
        "tags": [str(item.get("tag_name") or "") for item in tags],
    }


def readback(transport, max_pages: int = 100) -> dict:
    programs: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    reported_total: int | None = None
    page = 0
    while page < max_pages:
        # CAKE's Feed endpoint is one-based after the first page: both 0 and 1
        # return the first row, while the second 100-row page starts at 101.
        # Using 0, 100, 200... duplicated every page boundary and made an
        # incomplete catalogue look usable to downstream reconciliation.
        start = page * ROW_LIMIT + 1
        response = _request(transport, "GET", "/Offers/Feed",
                            params={"start_at_row": start, "row_limit": ROW_LIMIT,
                                    "sort_field": "offer_id", "sort_descending": False})
        if not response.ok:
            blockers.append({"page": page + 1, "http_status": response.status,
                             "error_class": "affiliati_offers_feed_http",
                             "body_excerpt": response.excerpt()})
            break
        document = response.json()
        rows = document.get("data") if isinstance(document, dict) else None
        rows = rows if isinstance(rows, list) else []
        if reported_total is None:
            try:
                reported_total = int(document.get("row_count"))
            except (TypeError, ValueError):
                reported_total = None
        programs.extend(normalize_offer(row) for row in rows if isinstance(row, dict))
        page += 1
        if not rows or len(rows) < ROW_LIMIT or (reported_total is not None and len(programs) >= reported_total):
            break
    ids = [str(program.get("program_id") or "") for program in programs]
    contracts = [str(program.get("offer_contract_id") or "") for program in programs]
    offer_counts: dict[str, int] = {}
    identity_counts: dict[tuple[str, str], int] = {}
    for program_id, contract_id in zip(ids, contracts):
        offer_counts[program_id] = offer_counts.get(program_id, 0) + 1
        identity = (program_id, contract_id)
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    # One CAKE offer can legitimately publish multiple payout contracts. Keep
    # those rows and expose the duplicated offer IDs for comparison; only an
    # identical (offer_id, offer_contract_id) pair is a pagination/data defect.
    duplicate_offer_ids = sorted(program_id for program_id, count in offer_counts.items()
                                 if program_id and count > 1)
    duplicate_identities = sorted(
        f"{program_id}:{contract_id}" for (program_id, contract_id), count in identity_counts.items()
        if program_id and contract_id and count > 1
    )
    if duplicate_identities:
        blockers.append({"error_class": "affiliati_duplicate_offer_contract_ids",
                         "duplicate_count": len(duplicate_identities),
                         "duplicate_identities": duplicate_identities[:100]})
    missing_program_id_count = offer_counts.get("", 0)
    missing_contract_id_count = sum(1 for contract_id in contracts if not contract_id)
    if missing_program_id_count or missing_contract_id_count:
        blockers.append({"error_class": "affiliati_missing_catalogue_identity",
                         "missing_program_id_count": missing_program_id_count,
                         "missing_contract_id_count": missing_contract_id_count})
    unique_offer_count = len([program_id for program_id in offer_counts if program_id])
    unique_contract_count = len([
        identity for identity in identity_counts if identity[0] and identity[1]
    ])
    complete = (reported_total is not None and len(programs) == reported_total
                and unique_contract_count == reported_total and not blockers)
    return b.readback_payload(
        DISPLAY, "PASS" if complete else "BLOCKED", programs,
        {"blockers": blockers, "pages_fetched": page, "reported_total": reported_total,
         "unique_offer_count": unique_offer_count,
         "unique_contract_count": unique_contract_count,
         "duplicate_offer_ids": duplicate_offer_ids,
         "complete": complete, "endpoint": f"GET {API}/Offers/Feed",
         "capability_split": {
             "offer_listing": "api", "program_detail_and_payout_terms": "api",
             "relationship_status": "api", "tracking_link_generation": "api_after_campaign",
             "application_submission": "api_mutation_and_portal",
             "pricing_terms_change_detection": "api_snapshot_diff",
         }})


def _relevant_approved_offer(program: dict[str, Any]) -> bool:
    if str(program.get("relationship") or "").casefold() != "approved":
        return False
    haystack = "\n".join(str(program.get(key) or "") for key in (
        "name", "vertical", "description", "restrictions", "advertiser_extended_terms",
    ))
    return bool(RELEVANT_OFFER_RE.search(haystack))


def _cookie_window(program: dict[str, Any], campaign: dict[str, Any]) -> dict[str, Any]:
    """Extract a disclosed cookie window without inventing a network default."""
    for source_name, value in (
        ("campaign_description", campaign.get("description")),
        ("campaign_restrictions", campaign.get("restrictions")),
        ("offer_description", program.get("description")),
        ("offer_restrictions", program.get("restrictions")),
        ("advertiser_extended_terms", program.get("advertiser_extended_terms")),
    ):
        text = str(value or "")
        match = re.search(
            r"\bcookie(?:\s+(?:window|duration|period))?\s*(?:is|:|=|-)?\s*(\d{1,4})\s*days?\b",
            text,
            re.I,
        )
        if match:
            return {"days": int(match.group(1)), "source": source_name,
                    "disclosed": True, "quote": match.group(0)}
    return {
        "days": None,
        "source": "not_disclosed_in_authenticated_offer_or_campaign_api",
        "disclosed": False,
        "quote": "",
    }


def _campaign_configuration(campaign_id: str, program_id: str, transport) -> dict[str, Any]:
    response = _request(
        transport, "GET", "/Offers/Campaign",
        params={"campaign_id": int(campaign_id)},
    )
    if not response.ok:
        raise RuntimeError(f"affiliati_campaign_configuration_http:{campaign_id}:{response.status}")
    document = response.json()
    rows = document.get("data") if isinstance(document, dict) else None
    rows = [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]
    matches = [row for row in rows
               if str(row.get("campaign_id") or "") == campaign_id
               and str(row.get("offer_id") or "") == program_id]
    if len(matches) != 1:
        raise RuntimeError(
            f"affiliati_campaign_configuration_identity_count:{program_id}:{campaign_id}:{len(matches)}"
        )
    return matches[0]


def _campaign_events(campaign_id: str, transport) -> list[dict[str, Any]]:
    response = _request(
        transport, "GET", "/Offers/Event",
        params={"campaign_id": int(campaign_id), "start_at_row": 1, "row_limit": 0},
    )
    if not response.ok:
        raise RuntimeError(f"affiliati_campaign_events_http:{campaign_id}:{response.status}")
    document = response.json()
    rows = document.get("data") if isinstance(document, dict) else None
    return [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]


def _default_creative(campaign: dict[str, Any]) -> dict[str, Any]:
    creatives = [row for row in campaign.get("creatives") or [] if isinstance(row, dict)]
    defaults = [row for row in creatives
                if "default" in str(row.get("creative_name") or "").casefold()
                and str((row.get("creative_type") or {}).get("creative_type_name") or "")
                    .casefold() == "link"]
    selected = defaults[0] if len(defaults) == 1 else (creatives[0] if len(creatives) == 1 else {})
    creative_id = str(selected.get("creative_id") or "")
    if not creative_id.isdigit():
        raise RuntimeError("affiliati_default_creative_not_unique_or_missing")
    return selected


def approved_discovery(transport, max_pages: int = 100) -> dict[str, Any]:
    """Enumerate joined relevant offers and inspect joined campaign configuration.

    This deliberately reads only Affiliati API configuration endpoints. The
    default Link creative's stored ``unique_link`` href is inspected; the
    tracking host is never requested, redirected, resolved, or clicked.
    """
    inventory = readback(transport, max_pages=max_pages)
    if inventory.get("auth_state") != "PASS" or inventory.get("complete") is not True:
        raise RuntimeError("affiliati_inventory_incomplete_for_approved_discovery")

    approved = [program for program in inventory.get("programs") or []
                if isinstance(program, dict) and _relevant_approved_offer(program)]
    offers: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    campaign_configuration_reads = 0
    campaign_event_reads = 0
    for program in approved:
        program_id = str(program.get("program_id") or "")
        campaign_id = str(program.get("campaign_id") or "")
        if not campaign_id.isdigit():
            blockers.append({"program_id": program_id,
                             "error_class": "approved_offer_campaign_id_missing"})
            continue
        try:
            campaign_configuration_reads += 1
            campaign = _campaign_configuration(campaign_id, program_id, transport)
            campaign_event_reads += 1
            events = _campaign_events(campaign_id, transport)
            selected_creative = _default_creative(campaign)
        except RuntimeError as exc:
            blockers.append({"program_id": program_id, "campaign_id": campaign_id,
                             "error_class": str(exc)})
            continue

        href = str(selected_creative.get("unique_link") or "").strip()
        parsed = urlsplit(href) if href else None
        href_structurally_present = bool(
            parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username
        )
        if not href_structurally_present:
            blockers.append({"program_id": program_id, "campaign_id": campaign_id,
                             "error_class": "campaign_link_creative_unique_link_missing_or_unsafe"})
        cookie = _cookie_window(program, campaign)
        offers.append({
            "program_id": program_id,
            "offer_contract_id": str(campaign.get("offer_contract_id")
                                      or program.get("offer_contract_id") or ""),
            "campaign_id": campaign_id,
            "name": str(campaign.get("offer_name") or program.get("name") or ""),
            "relationship": "approved",
            "status_name": str(campaign.get("status_name") or program.get("offer_status_raw") or ""),
            "commission": {
                "amount": campaign.get("price", program.get("headline_payout")),
                "type": str(campaign.get("price_format") or program.get("payout_model") or ""),
                "currency": str(program.get("currency") or ""),
                "display": str(program.get("payout_display") or ""),
            },
            "paid_events": events,
            "paid_event_from_offer_terms": str(program.get("paid_event") or ""),
            "geo": campaign.get("allowed_countries") or [],
            "cookie_window": cookie,
            "restrictions": str(campaign.get("restrictions") or program.get("restrictions") or ""),
            "allowed_media_types": campaign.get("allowed_media_types")
                                   or program.get("allowed_media_types") or [],
            "description": str(campaign.get("description") or program.get("description") or ""),
            "official_url": str(campaign.get("preview_link") or program.get("official_url") or ""),
            "tracking_href": href if href_structurally_present else "",
            "tracking_href_host": (parsed.hostname or "").casefold()
                                  if href_structurally_present else "",
            "tracking_href_source": (
                "GET /Offers/Campaign creatives[].unique_link configuration field inspection; "
                "href copied only, never requested"
            ),
            "tracking_href_structurally_present": href_structurally_present,
            "tracking_creative": {
                "creative_id": str(selected_creative.get("creative_id") or ""),
                "creative_name": str(selected_creative.get("creative_name") or ""),
                "creative_type": selected_creative.get("creative_type") or {},
                "unique_link": href,
            },
            "creative_configuration": campaign.get("creatives") or [],
            "campaign_configuration": {
                "expiration_date": campaign.get("expiration_date"),
                "creatives_download_url_present": bool(campaign.get("creatives_download_url")),
                "unsubscribe_link_present": bool(campaign.get("unsubscribe_link")),
                "suppression_link_present": bool(campaign.get("suppression_link")),
                "postback_url_present": bool(campaign.get("postback_url")),
                "pixel_html_present": bool(campaign.get("pixel_html")),
            },
        })

    return {
        "network": DISPLAY,
        "status": "PASS" if len(offers) == len(approved) and not blockers else "BLOCKED",
        "inventory_program_count": inventory.get("program_count"),
        "approved_relevant_count": len(approved),
        "captured_offer_count": len(offers),
        "offers": offers,
        "blockers": blockers,
        "source_endpoints": [
            f"GET {API}/Offers/Feed",
            f"GET {API}/Offers/Campaign",
            f"GET {API}/Offers/Event",

        ],
        "effects": {
            "applications_submitted": 0,
            "provider_mutations": 0,
            "tracking_links_requested": 0,
            "tracking_links_clicked": 0,
            "tracking_redirects_resolved": 0,
            "creative_code_configuration_reads": 0,
            "campaign_configuration_reads": campaign_configuration_reads,
            "campaign_event_reads": campaign_event_reads,
        },
    }


def offer(program_id: object, transport) -> dict[str, Any]:
    wanted = str(program_id).strip()
    result = readback(transport)
    matches = [row for row in result.get("programs") or [] if str(row.get("program_id")) == wanted]
    if len(matches) != 1:
        raise RuntimeError(f"affiliati_offer_identity_count:{wanted}:{len(matches)}")
    return matches[0]


def relationship(program_id, transport) -> dict:
    record = offer(program_id, transport)
    state = str(record.get("relationship") or "unknown")
    return {"present": state in {"approved", "pending"}, "state": state,
            "program_id": str(program_id), "campaign_id": record.get("campaign_id"),
            "record": record, "blockers": [],
            "provider_readback": "GET /Offers/Feed exact offer_id match"}


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    record = offer(program_id, transport)
    text = "\n\n".join(part for part in (
        f"Description: {record.get('description')}",
        f"Restrictions: {record.get('restrictions')}",
        f"Advertiser extended terms: {record.get('advertiser_extended_terms')}",
        f"Allowed media types: {', '.join(record.get('allowed_media_types') or [])}",
        f"Tags: {', '.join(record.get('tags') or [])}",
        f"Payout: {record.get('payout_display')}",
        f"Paid conversion event: {record.get('paid_event')}",
    ) if part.split(":", 1)[-1].strip())
    out = b.save_terms(
        NETWORK, str(program_id), text,
        sources=[{"kind": "affiliati_offers_feed_api",
                  "endpoint": f"GET {API}/Offers/Feed (exact offer_id={program_id})",
                  "chars": len(text)}],
        extra={"program_name": record.get("name"),
               "offer_contract_id": record.get("offer_contract_id"),
               "offer_status_raw": record.get("offer_status_raw"),
               "relationship": record.get("relationship"),
               "payout_display": record.get("payout_display"),
               "paid_event": record.get("paid_event"),
               "seo_allowed": record.get("seo_allowed")},
        terms_dir=terms_dir,
    )
    out["captured"] = True
    return out


def tracking_link(program: dict, subid: str = "site") -> dict:
    campaign_id = str(program.get("campaign_id") or "").strip()
    creative_id = str(program.get("creative_id") or "").strip()
    if campaign_id and creative_id:
        return b.link_lookup(
            ["python3", "scripts/marketplace_adapter_cli.py", "--network", NETWORK,
             "--program-id", str(program.get("program_id") or "")],
            f"GET {API}/Creatives/GetCode requires campaign_id={campaign_id} and creative_id={creative_id}; "
            f"append the site's subid={subid} only according to the returned code grammar")
    return b.link_lookup(
        ["python3", "scripts/marketplace_adapter_cli.py", "--network", NETWORK,
         "--readback"],
        "Affiliati tracking code is API-reachable through GET /Creatives/GetCode only after a "
        "campaign exists and a creative id is known; no link may be invented before approval")


def apply(program_id, transport=None, *, offer_contract_id=None, media_type_id=SEO_MEDIA_TYPE_ID,
          notes="", agreed_to_terms=False, agreed_from_ip_address="", authorize=False, **_kwargs):
    if not authorize:
        raise RuntimeError("affiliati_application_authority_required")
    if transport is None:
        transport = b.RequestsTransport()
    if not str(offer_contract_id or "").isdigit():
        raise RuntimeError("affiliati_offer_contract_id_required")
    if not str(notes or "").strip():
        raise RuntimeError("affiliati_promotion_notes_required")
    if agreed_to_terms is not True or not str(agreed_from_ip_address or "").strip():
        raise RuntimeError("affiliati_operator_terms_acceptance_and_source_ip_required")
    before = relationship(program_id, transport)
    if before["state"] in {"approved", "pending"}:
        return {"status": "NOOP_ALREADY_RELATED", "program_id": str(program_id),
                "provider_readback": before, "write_actions": 0, "secret_printed": False}
    body = {"offer_contract_id": int(offer_contract_id), "media_type_id": int(media_type_id),
            "notes": str(notes), "agreed_to_terms": True,
            "agreed_from_ip_address": str(agreed_from_ip_address)}
    response = _request(transport, "POST", "/Offers/Campaign/Create", json_body=body)
    if not response.ok:
        raise RuntimeError(f"affiliati_campaign_create_http:{response.status}")
    after = relationship(program_id, transport)
    if after["state"] not in {"pending", "approved"}:
        raise RuntimeError(f"affiliati_campaign_create_readback_unverified:{after['state']}")
    return {"status": "SUBMITTED_VERIFIED", "program_id": str(program_id),
            "provider_readback": after, "write_actions": 1, "secret_printed": False}


def join_adapter(config: dict, candidate: dict) -> dict:
    if config.get("affiliati_application_authority") != "operator_batch_authorized":
        raise RuntimeError("affiliati_application_authority_required")
    return apply(
        candidate.get("program_id"), b.RequestsTransport(),
        offer_contract_id=candidate.get("offer_contract_id"),
        media_type_id=candidate.get("media_type_id") or SEO_MEDIA_TYPE_ID,
        notes=candidate.get("promotion_notes"),
        agreed_to_terms=config.get("agreed_to_terms") is True,
        agreed_from_ip_address=config.get("agreed_from_ip_address"),
        authorize=True,
    )
