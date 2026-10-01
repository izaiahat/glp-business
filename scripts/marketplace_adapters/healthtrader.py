#!/usr/bin/env python3
"""HealthTrader adapter (OAuth2 client-credentials, api.healthtrader.com).

The account relationship is at NETWORK level (approved), and every offer the
account can see is promotable — so `readback` enumerates offers rather than
per-program approvals.

readback      GET /affiliates/v1/offers/list                      [live-proven]
capture_terms the offer record itself (payout, restrictions, geo, status) —
              HealthTrader publishes no separate terms endpoint.
tracking_link the offer record carries the tracking URL; subid is appended.
apply         NOT SUPPORTED — no application endpoint is documented and the
              vendor's Apiary docs host is unreachable (cert mismatch), so no
              endpoint can be cited. Offer access is account-level anyway.
"""
from __future__ import annotations

from . import _base as b

NETWORK = "healthtrader"
DISPLAY = "HealthTrader"
API = "https://api.healthtrader.com"
DOCS = ["http://docs.htaffiliate.apiary.io (documented in api_credentials.json; "
        "TLS cert does not cover this host — unreachable 2026-08-06)"]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "api", "apply": "deterministic"}
APPLY_FINDING = {
    "reason": "HealthTrader's affiliate relationship is granted at network-account level (approved "
              "2026-07-31); individual offers are visible/promotable without a per-offer application. "
              "No application endpoint is documented, and the documentation host recorded in our "
              "credential file (docs.htaffiliate.apiary.io) fails TLS hostname validation, so no "
              "endpoint can be cited — and guessing one is forbidden.",
    "citations": ["https://www.healthtrader.com/us/offers",
                  "local: ~/.hermes/secrets/affiliate-marketplaces/healthtrader/api_credentials.json documentation_url"],
    "portal_route": "affiliates.healthtrader.com (portal lane, not needed for offer access)",
}


def _token_paths() -> tuple:
    return ("affiliate-marketplaces", "healthtrader")


def access_token(transport, allow_mint: bool = True) -> str:
    tok = b.secret_json(*_token_paths(), "access_token.json")
    return str(tok.get("access_token") or "")


def mint_token(transport) -> dict:
    creds = b.secret_json(*_token_paths(), "api_credentials.json")
    resp = transport.request("POST", f"{API}/oauth/token",
                             data={"grant_type": "client_credentials",
                                   "client_id": creds["client_id"],
                                   "client_secret": creds["client_secret"], "scope": "*"})
    if not resp.ok:
        return {"ok": False, "http_status": resp.status, "body_excerpt": resp.excerpt(200)}
    minted = resp.json()
    record = {"schema": "healthtrader_oauth_access_token_v1",
              "access_token": minted["access_token"],
              "token_type": minted.get("token_type", "Bearer"),
              "scope": minted.get("scope", "*"), "issued_at_utc": b.utc(),
              "expires_in_seconds": int(minted.get("expires_in", 0)),
              "token_url": f"{API}/oauth/token"}
    b.write_private(b.SECRETS.joinpath(*_token_paths(), "access_token.json"), record)
    return {"ok": True, "http_status": resp.status, "access_token": minted["access_token"]}


def _offers(transport, token: str):
    return transport.request("GET", f"{API}/affiliates/v1/offers/list",
                             headers={"Authorization": f"Bearer {token}"})


def _rows(doc) -> list:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("data", "offers", "results", "items"):
            if isinstance(doc.get(key), list):
                return doc[key]
    return []


#: Provider-authored markers that mean "your bearer is no longer valid",
#: regardless of the HTTP status HealthTrader chose to wrap them in.
UNAUTHENTICATED_MARKERS = ("unauthenticated", "unauthorized", "token has expired",
                           "invalid token")


def _unauthenticated(resp) -> bool:
    body = (resp.text or "").lower()
    return any(marker in body for marker in UNAUTHENTICATED_MARKERS)


def readback(transport) -> dict:
    token = access_token(transport)
    resp = _offers(transport, token)
    refreshed = False
    # HealthTrader does NOT answer 401 for an expired bearer. Laravel Passport's
    # ValidateToken middleware raises, and the app serialises that as a 500 whose
    # body is {"error":{"Code":500,"Message":"Unauthenticated."}} (captured live
    # 2026-08-18). Refreshing only on 401 therefore never fired: the stale token
    # 500'd, readback returned BLOCKED with zero offers, and the supply lanes
    # recorded HealthTrader as an empty network for as long as the token had been
    # expired. Any status that carries the provider's own unauthenticated marker
    # now triggers exactly one mint-and-retry.
    if resp.status == 401 or (not resp.ok and _unauthenticated(resp)):
        minted = mint_token(transport)
        if minted.get("ok"):
            refreshed = True
            token = minted["access_token"]
            resp = _offers(transport, token)
    if not resp.ok:
        return b.readback_payload(DISPLAY, "BLOCKED", [], {
            "token_refreshed": refreshed,
            "blockers": [{"http_status": resp.status, "error_class": "healthtrader_offers_http",
                          "body_excerpt": resp.excerpt()}],
            "endpoint": f"GET {API}/affiliates/v1/offers/list"})
    programs = []
    for o in _rows(resp.json()):
        if not isinstance(o, dict):
            continue
        programs.append({"program_id": str(o.get("unique_id") or o.get("id") or ""),
                         "name": str(o.get("offer_name") or o.get("name") or ""),
                         "relationship": "available_via_approved_network_account",
                         "status": o.get("status"), "currency": o.get("currency"),
                         "url": str(o.get("tracking_url") or o.get("url") or ""),
                         "region": o.get("country") or o.get("geo") or "",
                         "description": str(o.get("description") or o.get("offer_description") or ""),
                         "categories": o.get("categories") or o.get("category") or [],
                         "countries": o.get("shipping_countries") or o.get("countries") or o.get("country")})
    return b.readback_payload(DISPLAY, "PASS", programs, {
        "token_refreshed": refreshed, "endpoint": f"GET {API}/affiliates/v1/offers/list",
        "reported_total": len(programs),
        "note": "relationship is network-account level (approved); offers/list is the promotable inventory."})


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    got = readback(transport)
    if got["auth_state"] != "PASS":
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "error_class": "healthtrader_offers_unavailable", "detail": got.get("blockers")}
    record = next((p for p in got["programs"] if p["program_id"] == str(program_id)), None)
    if not record:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "error_class": "healthtrader_offer_not_in_list"}
    text = " ".join(f"{k}: {v}" for k, v in record.items() if v not in (None, "", []))
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "healthtrader_offer_record_api",
                                 "endpoint": f"GET {API}/affiliates/v1/offers/list",
                                 "chars": len(text)}],
                       extra={"program_name": record.get("name"),
                              "separate_terms_endpoint": False,
                              "provider_record": record},
                       terms_dir=terms_dir)
    out["captured"] = True
    return out


def tracking_link(program: dict, subid: str = "site") -> dict:
    existing = str(program.get("tracking_url") or program.get("tracking_link") or "")
    if existing:
        sep = "&" if "?" in existing else "?"
        return b.link_resolved(f"{existing}{sep}{b.qs({'aff_sub': subid})}",
                               "healthtrader_offer_tracking_url_with_aff_sub")
    pid = str(program.get("program_id") or program.get("id") or "")
    if not pid:
        return b.link_unsupported("healthtrader requires offer id")
    return b.link_lookup(
        ["python3", "scripts/marketplace_adapter_cli.py", "--network", "healthtrader", "--readback"],
        f"the offer record from GET {API}/affiliates/v1/offers/list carries the tracking URL for offer {pid}")


def apply(program_id, transport, **kwargs):
    """Resolve account-level offer access without fabricating a provider write."""
    got = readback(transport)
    record = next((row for row in got.get("programs", [])
                   if row.get("program_id") == str(program_id)), None)
    if not record:
        return {"network": NETWORK, "program_id": str(program_id),
                "status": "SUBMISSION_UNVERIFIED", "write_actions": 0,
                "confirmation_state": "offer_not_visible_to_approved_account",
                "provider_readback": got, "secret_printed": False}
    return {"network": NETWORK, "program_id": str(program_id),
            "status": "ALREADY_RELATIONSHIP", "write_actions": 0,
            "confirmation_state": "available_via_approved_network_account",
            "provider_readback": record, "secret_printed": False}


def join_adapter(config: dict, candidate: dict) -> dict:
    return apply(candidate["program_id"], b.RequestsTransport())
