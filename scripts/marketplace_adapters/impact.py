#!/usr/bin/env python3
"""Impact.com media-partner adapter (Basic auth: AccountSid / AuthToken).

readback      GET /Mediapartners/{sid}/Campaigns.json                [live-proven]
capture_terms GET /Mediapartners/{sid}/Campaigns/{id}.json — the campaign record
              carries ContractStatus, PublicTerms/TermsAndConditions text and
              payout terms; screened through the standing rubric.
tracking_link the campaign record's TrackingLink field (lookup, then SubId1 is
              appended per Impact's documented subid parameter).
apply         NOT SUPPORTED. impact.com's Partner API "Programs" reference is
              GET-only (list / retrieve / logo); the POST that joins a program
              lives on the BRAND side (/Advertisers/{sid}/MediaPartners).
"""
from __future__ import annotations

from . import _base as b
from scripts.impact_api_credentials import load_impact_api_credentials

NETWORK = "impact"
DISPLAY = "Impact.com"
API = "https://api.impact.com"
DOCS = [
    "https://integrations.impact.com/partner-api-reference/reference/programs/programs",
    "https://integrations.impact.com/brand-api-reference/reference/partners/partners",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "api", "apply": "portal"}
APPLY_FINDING = {
    "reason": "impact.com's partner-side Programs reference documents only GET /Mediapartners/{sid}/Campaigns, "
              "GET /Mediapartners/{sid}/Campaigns/{id} and GET .../Logo — described as 'the brand programs "
              "you've already joined'. The only documented POST that creates a partner-program relationship is "
              "brand-side (POST /Advertisers/{sid}/MediaPartners), which we cannot call as a media partner.",
    "citations": ["https://integrations.impact.com/partner-api-reference/reference/programs/programs",
                  "https://integrations.impact.com/brand-api-reference/reference/partners/partners"],
    "portal_route": "app.impact.com marketplace apply (portal lane)",
}


def credentials() -> tuple[str, str]:
    credential = load_impact_api_credentials()
    return credential.basic_auth


def _relationship(contract_status: str) -> str:
    low = str(contract_status or "").strip().lower()
    if low in {"active", "joined", "approved"}:
        return "approved"
    if any(x in low for x in ("pending", "requested", "awaiting")):
        return "pending"
    if low == "never joined":
        return "notjoined"
    return low or "unknown"


def readback(transport, max_pages: int = 20) -> dict:
    credential = load_impact_api_credentials()
    sid, token = credential.basic_auth
    api = credential.api_base
    programs, blockers = [], []
    page = 1
    while page <= max_pages:
        resp = transport.request("GET", f"{api}/Mediapartners/{sid}/Campaigns.json",
                                 params={"PageSize": 100, "Page": page}, auth=(sid, token))
        if not resp.ok:
            blockers.append({"page": page, "http_status": resp.status,
                             "error_class": "impact_campaigns_http", "body_excerpt": resp.excerpt()})
            break
        body = resp.json()
        rows = body.get("Campaigns") or []
        for c in rows:
            programs.append({
                "program_id": str(c.get("CampaignId") or ""),
                "name": str(c.get("CampaignName") or c.get("AdvertiserName") or ""),
                "relationship": _relationship(c.get("ContractStatus")),
                "contract_status_raw": c.get("ContractStatus"),
                "url": str(c.get("CampaignUrl") or c.get("AdvertiserUrl") or ""),
                "tracking_link": str(c.get("TrackingLink") or ""),
            })
        total_pages = int(body.get("@numpages") or 1)
        if page >= total_pages or not rows:
            break
        page += 1
    return b.readback_payload(
        DISPLAY, "BLOCKED" if blockers and not programs else "PASS", programs,
        {"account_sid_present": True, "blockers": blockers, "pages_fetched": page,
         "endpoint": f"GET {API}/Mediapartners/<sid>/Campaigns.json"})


def campaign(program_id, transport) -> dict:
    credential = load_impact_api_credentials()
    sid, token = credential.basic_auth
    resp = transport.request("GET", f"{credential.api_base}/Mediapartners/{sid}/Campaigns/{program_id}.json",
                             auth=(sid, token))
    return {"http_status": resp.status, "record": resp.json() if resp.ok else None,
            "body_excerpt": None if resp.ok else resp.excerpt()}


def relationship(program_id, transport) -> dict:
    """Fresh exact campaign relationship from the Partner API."""
    got = campaign(program_id, transport)
    if got["http_status"] == 404:
        return {"present": False, "state": "notjoined", "program_id": str(program_id),
                "http_status": 404, "provider_readback": "GET partner Campaign detail"}
    record = got.get("record") or {}
    state = _relationship(record.get("ContractStatus")) if record else "readback_blocked"
    return {"present": bool(record) and state in {"approved", "pending", "joined", "active"},
            "state": state, "program_id": str(program_id), "http_status": got["http_status"],
            "blockers": [] if record or got["http_status"] == 404 else [f"campaign_http_{got['http_status']}"],
            "provider_readback": "GET partner Campaign detail"}


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    got = campaign(program_id, transport)
    if not got["record"]:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": got["http_status"], "error_class": "impact_campaign_http",
                "body_excerpt": got["body_excerpt"]}
    rec = got["record"]
    fields = ("CampaignName", "AdvertiserName", "ContractStatus", "PublicTermsAndConditions",
              "TermsAndConditions", "CampaignDescription", "PayoutTerms", "AllowedCountries",
              "ContractCurrency", "CookiePolicy", "PromotionalPolicy")
    parts = [f"{k}: {rec[k]}" for k in fields if rec.get(k)]
    for payout in (rec.get("Payouts") or []) if isinstance(rec.get("Payouts"), list) else []:
        parts.append(f"Payout {payout.get('ActionTrackerName')}: {payout.get('Payout')} "
                     f"{payout.get('PayoutType')}.")
    text = " ".join(str(p) for p in parts)
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "impact_campaign_record_api",
                                 "endpoint": f"GET {API}/Mediapartners/<sid>/Campaigns/{program_id}.json",
                                 "chars": len(text)}],
                       extra={"program_name": rec.get("CampaignName"),
                              "contract_status": rec.get("ContractStatus"),
                              "provider_record_fields": sorted(rec.keys())},
                       terms_dir=terms_dir)
    out["captured"] = True
    return out


def tracking_link(program: dict, subid: str = "site") -> dict:
    existing = str(program.get("tracking_link") or "")
    pid = str(program.get("program_id") or program.get("id") or "")
    if existing:
        sep = "&" if "?" in existing else "?"
        return b.link_resolved(f"{existing}{sep}subId1={b.quote(subid)}",
                               "impact_campaign_tracking_link_with_subid1")
    if not pid:
        return b.link_unsupported("impact requires campaign id")
    return b.link_lookup(
        ["python3", "scripts/marketplace_adapter_cli.py", "--network", "impact",
         "--terms", "--program-id", pid],
        f"TrackingLink is a field on GET {API}/Mediapartners/<sid>/Campaigns/{pid}.json; "
        "the readback receipt already carries it for every joined campaign")


def apply(program_id, transport=None, **kwargs):
    raise b.NotSupported(NETWORK, "apply", APPLY_FINDING["reason"], APPLY_FINDING["citations"])
