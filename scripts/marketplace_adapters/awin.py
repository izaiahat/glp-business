#!/usr/bin/env python3
"""Awin publisher adapter (publisher 2979387).

readback      GET /publishers/{pid}/programmes?relationship=<rel>   [live-proven]
capture_terms GET /publishers/{pid}/programmes/details?advertiserId= [live-proven]
tracking_link deterministic awin1.com construction — UNCHANGED from the standing
              resolver on purpose: the cread.php vs awclick.php builder question
              is owned by the g6-r1a wire-in mission, not by this adapter.
              `linkbuilder_generate()` implements Awin's documented Link Builder
              API (POST .../linkbuilder/generate) but is NOT called live here for
              the same ownership reason.
apply         NOT SUPPORTED — Awin's published API index has no publisher
              join/apply endpoint. Joining is a portal action
              (scripts/awin_exact_application_adapter.py).
"""
from __future__ import annotations

from . import _base as b

NETWORK = "awin"
DISPLAY = "Awin"
PUBLISHER_ID = "2979387"
API = "https://api.awin.com"
RELATIONSHIPS = ("joined", "pending", "notjoined", "rejected", "suspended")
DOCS = [
    "https://help.awin.com/apidocs/for-publishers.md",
    "https://help.awin.com/apidocs/get-program-information-for-publisher-by-relationship-and-optionally-filter-by-country.md",
    "https://help.awin.com/apidocs/get-program-information-details-for-publisher.md",
    "https://help.awin.com/apidocs/generatelink.md",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "deterministic", "apply": "none"}
APPLY_FINDING = {
    "reason": "Awin's published publisher API surface (help.awin.com/llms.txt apidocs index) exposes "
              "commissions, linkbuilder, reports, transactions, promotions, programme information and "
              "user APIs only. No join/apply mutation is documented for publishers.",
    "citations": ["https://help.awin.com/llms.txt", "https://help.awin.com/apidocs/for-publishers.md"],
    "portal_route": "scripts/awin_exact_application_adapter.py (fail-closed, gate-driven, one merchant per run)",
}


def _headers() -> dict:
    return {"Authorization": "Bearer " + b.secret_json("affiliate-marketplaces", "awin", "api.json")["api_key"]}


def readback(transport, relationships: tuple = RELATIONSHIPS) -> dict:
    programs, blockers = [], []
    for rel in relationships:
        resp = transport.request("GET", f"{API}/publishers/{PUBLISHER_ID}/programmes",
                                 headers=_headers(), params={"relationship": rel})
        if not resp.ok:
            blockers.append({"relationship": rel, "http_status": resp.status,
                             "error_class": "awin_programmes_http", "body_excerpt": resp.excerpt()})
            continue
        rows = resp.json()
        for r in rows if isinstance(rows, list) else []:
            region = r.get("primaryRegion") or {}
            programs.append({
                "program_id": str(r.get("id") or ""), "name": str(r.get("name") or ""),
                "relationship": rel, "region": str(region.get("countryCode") or ""),
                # Advertiser status is independent of our membership bucket.
                # Preserve the API's exact value; never infer Active from listing.
                "network_status": str(r.get("status") or ""),
                "currency": str(r.get("currencyCode") or ""), "url": str(r.get("displayUrl") or ""),
                "description": str(r.get("description") or "")[:500],
                # Awin's own sector taxonomy. Carried so the niche lanes see the
                # network's classification instead of inferring one from a
                # 255-char marketing blurb.
                "categories": [s for s in [str(r.get("primarySector") or "")] if s],
            })
    return b.readback_payload(
        DISPLAY, "BLOCKED" if blockers and not programs else "PASS", programs,
        {"publisher_id": PUBLISHER_ID, "blockers": blockers,
         "endpoint": f"GET {API}/publishers/{PUBLISHER_ID}/programmes?relationship="})


def _programme_text(detail: dict) -> str:
    info = detail.get("programmeInfo") or {}
    parts = [f"Programme: {info.get('name')} (advertiser {info.get('id')}).",
             f"Membership status: {info.get('status')}.",
             f"Currency: {info.get('currencyCode')}.",
             f"Valid domains: {', '.join(str(d.get('domain')) for d in (info.get('validDomains') or []))}.",
             f"Deeplink enabled: {info.get('deeplinkEnabled')}.",
             f"Cookie period: {(detail.get('kpi') or {}).get('cookiePeriod')}.",
             f"Description: {info.get('description') or ''}"]
    for group in (detail.get("commissionRange") or []):
        parts.append(f"Commission {group.get('type')}: min {group.get('min')} max {group.get('max')}.")
    for key, value in (detail.get("kpi") or {}).items():
        parts.append(f"KPI {key}: {value}.")
    return " ".join(str(p) for p in parts)


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    """Capture the Awin programme record and screen it.

    Honest scope: Awin serves the *legal* programme T&C as a portal PDF behind
    the advertiser profile (TOTP-gated, operator-owned). This capture is the
    API-visible commercial record — commission ranges, cookie period, domains,
    membership status — screened through the standing rubric. The limitation is
    recorded in the receipt rather than papered over.
    """
    resp = transport.request("GET", f"{API}/publishers/{PUBLISHER_ID}/programmedetails",
                             headers=_headers(), params={"advertiserId": str(program_id)})
    if not resp.ok:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "awin_programmedetails_http",
                "body_excerpt": resp.excerpt()}
    detail = resp.json()
    text = _programme_text(detail)
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "awin_programme_details_api",
                                 "endpoint": f"GET {API}/publishers/{PUBLISHER_ID}/programmedetails?advertiserId={program_id}",
                                 "chars": len(text)}],
                       extra={"program_name": ((detail.get("programmeInfo") or {}).get("name")),
                              "legal_terms_pdf_captured": False,
                              "legal_terms_note": "Awin programme T&C PDF is portal-only (TOTP-gated); "
                                                  "API capture covers the commercial programme record.",
                              "provider_record": detail},
                       terms_dir=terms_dir)
    out["captured"] = True
    return out


def tracking_link(program: dict, subid: str = "site") -> dict:
    pid = str(program.get("program_id") or program.get("id") or "")
    if not pid:
        return b.link_unsupported("awin requires programme id")
    dest = str(program.get("destination_url") or program.get("url") or "")
    url = "https://www.awin1.com/cread.php?" + b.qs(
        {"awinmid": pid, "awinaffid": PUBLISHER_ID, "clickref": subid})
    if dest:
        url += "&ued=" + b.quote(dest)
    return b.link_resolved(url, "awin_cread_deterministic")


def linkbuilder_generate(program: dict, transport, subid: str = "site") -> dict:
    """Awin's documented Link Builder API. Implemented, deliberately not run live.

    POST /publishers/{publisherId}/linkbuilder/generate
    body: {"advertiserId": int, "destinationUrl": str, "parameters": {"clickref": ...}}
    Ownership: the awin builder-form question belongs to the g6-r1a wire-in
    mission; this method exists so that mission (or a later run) can flip Awin
    from a constructed link to a provider-authoritative one without new code.
    """
    body = {"advertiserId": int(program["program_id"]),
            "parameters": {"clickref": subid}}
    dest = str(program.get("destination_url") or program.get("url") or "")
    if dest:
        body["destinationUrl"] = dest
    resp = transport.request("POST", f"{API}/publishers/{PUBLISHER_ID}/linkbuilder/generate",
                             headers=_headers(), json_body=body)
    if not resp.ok:
        return {"state": "blocked", "http_status": resp.status, "body_excerpt": resp.excerpt()}
    doc = resp.json()
    return b.link_resolved(str(doc.get("url") or ""), "awin_linkbuilder_api")


def apply(program_id, transport=None, **kwargs):
    raise b.NotSupported(NETWORK, "apply", APPLY_FINDING["reason"], APPLY_FINDING["citations"])
