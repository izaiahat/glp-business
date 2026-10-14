#!/usr/bin/env python3
"""Digistore24 affiliate adapter (X-DS-API-KEY auth).

listMarketplaceEntries lists vendor-owned marketplace data, not the full
affiliate marketplace. validateAffiliate verifies one product affiliation.
There is no affiliate-side application mutation API:
`requestMarketplaceEntryApproval` is vendor-owned. A returned `invite_url` is
therefore handed to the authenticated portal executor, which performs at most
one UI submit and then verifies with `validateAffiliate`.
"""
from __future__ import annotations

import re

from . import _base as b

NETWORK = "digistore24"
DISPLAY = "Digistore24"
API = "https://www.digistore24.com/api/call"
REDIR = "https://www.checkout-ds24.com/redir"
AFFILIATE_ID = "tiltongroup"
DOCS = [
    "https://dev.digistore24.com/en/articles/50-listmarketplaceentries",
    "https://dev.digistore24.com/en/articles/56-validateaffiliate",
    "https://help.digistore24.com/hc/en-us/articles/24293091859985-Promolink",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "deterministic", "apply": "portal"}
APPLY_FINDING = {
    "reason": "Digistore24 exposes affiliate relationship readback through validateAffiliate but no "
              "affiliate-side application mutation. requestMarketplaceEntryApproval is vendor-owned; "
              "the provider-returned invite_url is the bounded portal fallback.",
    "citations": DOCS[:2],
    "portal_route": "validateAffiliate invite_url (authenticated portal lane)",
}
_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _headers() -> dict:
    return {"X-DS-API-KEY": b.secret_text("glpclinicscout", "digistore24_api_key")}


def _call(transport, fn: str, params: dict | None = None):
    resp = transport.request("GET", f"{API}/{fn}", headers=_headers(), params=params)
    body = resp.json() if resp.text.strip().startswith(("{", "[")) else {"raw_excerpt": resp.excerpt(200)}
    return resp, body


def _entries(body) -> list[dict]:
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        for key in ("entries", "items", "products"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


#: The ONLY sort keys Digistore24 accepts, quoted verbatim from the provider's
#: own 400 response (2026-08-18):
#:   "invalid value for 1. argument 'sort_by' (given: 'popularity' -
#:    allowed: name, stars, created, rank, profit, cancel, conversion, revenue)"
#: The adapter shipped with `popularity`, which is NOT in that set, so every
#: Digistore24 enumeration since the adapter was written returned HTTP 400 and
#: zero programs — and the daily "marketplace exhausted" verdict was counting a
#: silent provider error as an empty catalog. `rank` is Digistore24's own
#: marketplace-order default and is inside the allowed set.
MARKETPLACE_SORT_KEYS = ("name", "stars", "created", "rank", "profit",
                         "cancel", "conversion", "revenue")


def marketplace(transport, *, sort_by: str = "rank") -> dict:
    """Read vendor-owned entries; the affiliate marketplace denominator is unknown."""
    if sort_by not in MARKETPLACE_SORT_KEYS:
        raise ValueError(
            f"digistore24 sort_by={sort_by!r} is not accepted by the provider; "
            f"allowed: {', '.join(MARKETPLACE_SORT_KEYS)}")
    resp, body = _call(transport, "listMarketplaceEntries", {"sort_by": sort_by})
    rows = _entries(body)
    programs = []
    for item in rows:
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        pid = str(item.get("product_id") or item.get("id") or product.get("id") or "").strip()
        name = str(item.get("name") or item.get("product_name") or product.get("name") or "").strip()
        if not pid:
            continue
        categories = item.get("categories") or item.get("category") or product.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        description = str(item.get("description") or item.get("short_description")
                          or product.get("description") or "")
        programs.append({
            "program_id": pid, "name": name, "relationship": "unknown_affiliation",
            "description": description[:2000],
            "categories": [str(x) for x in categories if str(x).strip()],
            "region": str(item.get("country") or item.get("region") or ""),
            "currency": str(item.get("currency") or product.get("currency") or ""),
            "url": str(item.get("salespage_url") or item.get("url") or product.get("url") or ""),
            "application_url": str(item.get("affiliate_invite_url") or item.get("invite_url") or ""),
            "provider_record": item,
        })
    reported = None
    if isinstance(body, dict):
        data = body.get("data") or {}
        if isinstance(data, dict):
            reported = next((data[k] for k in ("total", "count", "total_count")
                             if k in data and data[k] is not None), None)
    return {"ok": resp.ok and isinstance(body, dict) and body.get("result") in (None, "success"),
            "http_status": resp.status, "programs": programs,
            "scope": "vendor_owned_marketplace_entries", "affiliate_id": AFFILIATE_ID,
            "scoped_reported_total": int(str(reported)) if str(reported).isdigit() else None,
            "reported_total": None, "full_marketplace_total": None,
            "endpoint": f"GET {API}/listMarketplaceEntries"}


def account_name(transport) -> str:
    _resp, body = _call(transport, "getUserInfo")
    data = (body or {}).get("data") or {}
    for key in ("user_name", "username", "login", "name", "account", "id", "user_id"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return AFFILIATE_ID


def affiliation(program_id, transport, affiliate_name: str = "") -> dict:
    affiliate = affiliate_name or account_name(transport)
    resp, body = _call(transport, "validateAffiliate", {
        "affiliate_name": affiliate, "product_ids": str(program_id)})
    data = body.get("data") if isinstance(body, dict) else {}
    data = data if isinstance(data, dict) else {}
    status = str(data.get("affiliation_status") or "").strip().lower()
    present = str(data.get("have_affiliation") or "").upper() == "Y" or status == "approved"
    return {"program_id": str(program_id), "present": present, "state": status or "unknown",
            "invite_url": str(data.get("invite_url") or ""), "http_status": resp.status,
            "provider_readback": f"GET {API}/validateAffiliate", "secret_printed": False}


def readback(transport) -> dict:
    catalog = marketplace(transport)
    programs = catalog["programs"]
    return b.readback_payload(
        DISPLAY, "PASS" if catalog["ok"] else "BLOCKED", programs,
        {"endpoint": catalog["endpoint"], "reported_total": catalog["reported_total"],
         "scope": catalog["scope"], "affiliate_id": AFFILIATE_ID,
         "scoped_reported_total": catalog["scoped_reported_total"],
         "full_marketplace_total": None,
         "marketplace_model": "vendor_owned_entries_plus_per_product_affiliation_readback",
         "blockers": [{"error_class": "authorized_vault_browser_required",
                       "reason": "vendor_owned_entries_do_not_enumerate_affiliate_marketplace"}]
                     + ([] if catalog["ok"] else [{"error_class": "digistore24_marketplace_http",
                                                   "http_status": catalog["http_status"]}])})


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    resp, body = _call(transport, "getProduct", {"product_id": str(program_id)})
    if not resp.ok or (isinstance(body, dict) and body.get("result") != "success"):
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "digistore24_get_product_failed",
                "body_excerpt": resp.excerpt(300)}
    data = body.get("data") or {}
    def flatten(node, prefix=""):
        out = []
        if isinstance(node, dict):
            for key, value in node.items():
                out.extend(flatten(value, f"{prefix}{key}."))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                out.extend(flatten(value, f"{prefix}{index}."))
        elif node not in (None, ""):
            out.append(f"{prefix.rstrip('.')}: {node}")
        return out
    text = " ".join(flatten(data))
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "digistore24_get_product_api",
                                 "endpoint": f"GET {API}/getProduct?product_id={program_id}",
                                 "chars": len(text)}],
                       extra={"provider_record": data}, terms_dir=terms_dir)
    out["captured"] = True
    return out


def tracking_link(program: dict, subid: str = "site") -> dict:
    product = str(program.get("program_id") or program.get("id") or "").strip()
    affiliate = str(program.get("affiliate") or program.get("affiliate_id") or AFFILIATE_ID).strip()
    if not product:
        return b.link_unsupported("digistore24 requires the product id as program_id")
    campaign = re.sub(r"[^A-Za-z0-9_-]", "-", subid)[:64] or "site"
    if not _SAFE.match(product) or not _SAFE.match(affiliate):
        return b.link_unsupported("digistore24 product/affiliate identifiers must be url-path safe")
    return b.link_resolved(f"{REDIR}/{product}/{affiliate}/{campaign}",
                           "digistore24_promolink_deterministic")


def apply(program_id, transport=None, **kwargs):
    raise b.NotSupported(NETWORK, "apply", APPLY_FINDING["reason"], APPLY_FINDING["citations"])
