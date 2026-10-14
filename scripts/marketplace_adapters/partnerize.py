#!/usr/bin/env python3
"""Partnerize partner adapter (publisher 1100l432092).

The only network in this portfolio that publishes a partner-side JOIN mutation.

readback      GET /user/publisher/{pid}/campaign/{status}            [live-proven]
capture_terms GET /v2/publishers/{pid}/campaigns/terms?campaigns=    [documented]
tracking_link deterministic prf.hn when the camref is known; camrefs come from
              GET /reference/publisher/camref/{pid} (CSV: Advertiser,Campaign,camref)
apply         POST /v2/publishers/{pid}/campaign-requests            [documented]
              body {"campaign_requests":[{"advertiser_id":..,"campaign_id":..}]}
              201 -> {"success_count":N,"failure_count":N,...}

Auth: HTTP Basic (user_application_key, user_api_key).
"""
from __future__ import annotations

import csv
import io
import re


from . import _base as b

NETWORK = "partnerize"
DISPLAY = "Partnerize"
PUBLISHER_ID = "1100l432092"
API = "https://api.partnerize.com"
DOCS = [
    "https://api-docs.partnerize.com/partner/",
    "https://api-docs.partnerize.com/partner/#tag/Campaign-Requests-(v2)",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "deterministic", "apply": "api"}
APPLY_ENDPOINT = f"POST {API}/v2/publishers/{PUBLISHER_ID}/campaign-requests"
# Partnerize's campaign endpoint accepts exactly a(pproved), p(ending),
# r(ejected) and (t) = pending terms & conditions — it 400s on anything else.
# "joined" was in this tuple and had been 400ing on every run, while "t" (a real
# state, and one that blocks an application) was never swept at all.
STATUSES = ("approved", "pending", "rejected", "t")


def _auth() -> tuple:
    sec = b.secret_json("affiliate-marketplaces", "partnerize", "api.json")
    return (sec["user_application_key"], sec["user_api_key"])


def readback(transport, statuses: tuple = STATUSES) -> dict:
    programs, blockers = [], []
    seen: set[str] = set()
    for status in statuses:
        resp = transport.request("GET", f"{API}/user/publisher/{PUBLISHER_ID}/campaign/{status}",
                                 auth=_auth())
        if not resp.ok:
            blockers.append({"status": status, "http_status": resp.status,
                             "error_class": "partnerize_campaign_status_http",
                             "body_excerpt": resp.excerpt()})
            continue
        doc = resp.json()
        rows = doc.get("campaigns") if isinstance(doc, dict) else doc
        for row in rows if isinstance(rows, list) else []:
            camp = row.get("campaign") if isinstance(row.get("campaign"), dict) else row
            cid = str(camp.get("campaign_id") or camp.get("id") or "")
            key = f"{status}:{cid}"
            if not cid or key in seen:
                continue
            seen.add(key)
            programs.append({
                "program_id": cid,
                "advertiser_id": str(camp.get("advertiser_id") or ""),
                "name": str(camp.get("title") or camp.get("name") or ""),
                "relationship": "approved" if status in {"approved", "joined"} else status,
                "region": str(camp.get("country") or ""), "currency": str(camp.get("currency") or ""),
                "url": str(camp.get("url") or camp.get("destination_url") or ""),
            })
    return b.readback_payload(
        DISPLAY, "BLOCKED" if blockers and not programs else "PASS", programs,
        {"publisher_id": PUBLISHER_ID, "blockers": blockers,
         "endpoint": f"GET {API}/user/publisher/{PUBLISHER_ID}/campaign/<status>"})



def discovery(transport) -> dict:
    """Official Brand Discovery v2 catalogue (read-only, publisher-scoped)."""
    resp = transport.request("GET", f"{API}/v2/publishers/{PUBLISHER_ID}/discovery/advertisers",
                             auth=_auth())
    if not resp.ok:
        return {"status": "EVIDENCED_BLOCKED", "http_status": resp.status,
                "advertisers": [], "body_excerpt": resp.excerpt(),
                "endpoint": f"GET {API}/v2/publishers/{PUBLISHER_ID}/discovery/advertisers",
                "effects": {"provider_mutations": 0, "tracking_clicks": 0},
                "secret_printed": False}
    doc = resp.json()
    rows, blockers = [], []
    endpoint = f"GET {API}/v2/publishers/{PUBLISHER_ID}/discovery/advertisers"
    advertisers = doc.get("advertisers") if isinstance(doc, dict) else None
    if not isinstance(advertisers, list):
        blockers.append({"error_class": "partnerize_discovery_invalid_advertisers"})
        advertisers = []
    for adv in advertisers:
        aid = adv.get("id") if isinstance(adv, dict) else None
        campaigns = adv.get("campaigns") if isinstance(adv, dict) else None
        if not isinstance(aid, str) or not aid or not isinstance(campaigns, list):
            blockers.append({"error_class": "partnerize_discovery_invalid_advertiser"})
            continue
        for campaign in campaigns:
            cid = campaign.get("id") if isinstance(campaign, dict) else None
            if not isinstance(cid, str) or not cid:
                blockers.append({"error_class": "partnerize_discovery_invalid_campaign_id",
                                 "advertiser_id": aid})
                continue
            countries = campaign.get("promotional_countries") or []
            vertical = campaign.get("vertical") or {}
            currency = campaign.get("currency") or {}
            rows.append({
                "advertiser_id": aid, "campaign_id": cid,
                "name": campaign.get("title") or "",
                "advertiser_name": adv.get("display_name") or adv.get("name") or "",
                "description": adv.get("description") or "",
                "url": campaign.get("default_destination") or "",
                "network_status": campaign.get("status") or "",
                "categories": [vertical["name"]] if vertical.get("name") else [],
                "vertical": vertical, "promotional_countries": countries,
                "region": ",".join(c["iso"] for c in countries if c.get("iso")),
                "currency": currency.get("iso") or "", "publisher_id": PUBLISHER_ID,
                "discovery_provenance": {"endpoint": endpoint, "publisher_id": PUBLISHER_ID,
                                         "advertiser_id": aid, "campaign_id": cid},
            })
    return {"status": "EVIDENCED_BLOCKED" if blockers else ("PASS" if rows else "EVIDENCED_EMPTY"),
            "http_status": resp.status, "advertisers": rows,
            "publisher_id": PUBLISHER_ID, "advertiser_count": len(advertisers),
            "advertiser_unique_count": len({a['id'] for a in advertisers
                                            if isinstance(a, dict) and isinstance(a.get('id'), str)}),
            "campaign_count": len(rows), "campaign_unique_count": len({r['campaign_id'] for r in rows}),
            "reported_advertiser_total": doc.get("count") if isinstance(doc, dict) else None,
            "reported_campaign_total": None, "blockers": blockers,
            "endpoint": endpoint,
            "effects": {"provider_mutations": 0, "tracking_clicks": 0},
            "secret_printed": False}


def _strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(raw or ""))).strip()


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    resp = transport.request("GET", f"{API}/v2/publishers/{PUBLISHER_ID}/campaigns/terms",
                             params={"campaigns": str(program_id)}, auth=_auth())
    if not resp.ok:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "partnerize_campaign_terms_http",
                "body_excerpt": resp.excerpt()}
    doc = resp.json()
    blocks = doc.get("terms") if isinstance(doc, dict) else None
    if not blocks:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "partnerize_campaign_terms_empty",
                "body_excerpt": resp.excerpt(200)}
    text = "\n\n".join(f"{t.get('title')}: {_strip_html(t.get('content'))}" for t in blocks)
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "partnerize_campaign_terms_api",
                                 "endpoint": f"GET {API}/v2/publishers/{PUBLISHER_ID}/campaigns/terms?campaigns={program_id}",
                                 "term_blocks": len(blocks), "chars": len(text)}],
                       extra={"term_titles": [t.get("title") for t in blocks]},
                       terms_dir=terms_dir)
    out["captured"] = True
    return out


def camrefs(transport) -> dict:
    """GET /reference/publisher/camref/{pid} -> CSV Advertiser,Campaign,camref."""
    resp = transport.request("GET", f"{API}/reference/publisher/camref/{PUBLISHER_ID}", auth=_auth())
    if not resp.ok:
        return {"ok": False, "http_status": resp.status, "body_excerpt": resp.excerpt()}
    rows = list(csv.reader(io.StringIO(resp.text)))
    has_header = bool(rows) and "camref" in ",".join(rows[0]).lower()
    out = []
    for row in (rows[1:] if has_header else rows):
        if len(row) >= 3:
            out.append({"advertiser": row[0].strip(), "campaign": row[1].strip(),
                        "camref": row[2].strip()})
    return {"ok": True, "http_status": resp.status, "camrefs": out}


def tracking_link(program: dict, subid: str = "site") -> dict:
    camref = str(program.get("camref") or "")
    dest = str(program.get("destination_url") or program.get("url") or "")
    if camref:
        url = f"https://prf.hn/click/camref:{camref}/pubref:{subid}"
        if dest:
            url += "/destination:" + b.quote(dest)
        return b.link_resolved(url, "partnerize_prf_deterministic")
    pid = str(program.get("program_id") or program.get("id") or "")
    if not pid:
        return b.link_unsupported("partnerize requires campaign id or camref")
    return b.link_lookup(
        ["python3", "scripts/marketplace_adapter_cli.py", "--network", "partnerize", "--camrefs"],
        f"camref for campaign {pid} comes from GET {API}/reference/publisher/camref/{PUBLISHER_ID}; "
        "the prf.hn link is deterministic once the camref is known")


def relationship(program_id, transport) -> dict:
    """Read one exact campaign relationship from provider-owned status buckets."""
    wanted = str(program_id)
    observed, blockers = [], []
    for status in ("approved", "pending", "t"):
        resp = transport.request("GET", f"{API}/user/publisher/{PUBLISHER_ID}/campaign/{status}",
                                 auth=_auth())
        if not resp.ok:
            blockers.append({"status": status, "http_status": resp.status})
            continue
        doc = resp.json()
        rows = doc.get("campaigns") if isinstance(doc, dict) else doc
        for row in rows if isinstance(rows, list) else []:
            campaign = row.get("campaign") if isinstance(row.get("campaign"), dict) else row
            if str(campaign.get("campaign_id") or campaign.get("id") or "") == wanted:
                observed.append(status)
    state = observed[0] if len(set(observed)) == 1 else ""
    return {"present": bool(state), "state": state, "program_id": wanted,
            "statuses_checked": ["approved", "pending", "t"], "blockers": blockers,
            "provider_readback": f"GET {API}/user/publisher/{PUBLISHER_ID}/campaign/<status>"}


def apply(program_id, transport, advertiser_id: str = "", **kwargs) -> dict:
    """Submit one campaign request and require provider-owned post-readback."""
    if not advertiser_id:
        raise ValueError("partnerize apply requires advertiser_id (documented request field)")
    before = relationship(program_id, transport)
    if before["present"]:
        return {"network": NETWORK, "program_id": str(program_id),
                "advertiser_id": str(advertiser_id), "status": "ALREADY_RELATIONSHIP",
                "relationship_state": before["state"], "write_actions": 0,
                "provider_readback": before, "secret_printed": False}
    if before.get("blockers"):
        return {"network": NETWORK, "program_id": str(program_id),
                "advertiser_id": str(advertiser_id), "status": "SUBMISSION_UNVERIFIED",
                "confirmation_state": "provider_pre_readback_blocked", "write_actions": 0,
                "provider_readback": before, "secret_printed": False}
    body = {"campaign_requests": [{"advertiser_id": str(advertiser_id),
                                   "campaign_id": str(program_id)}]}
    resp = transport.request("POST", f"{API}/v2/publishers/{PUBLISHER_ID}/campaign-requests",
                             auth=_auth(), json_body=body,
                             headers={"Content-Type": "application/json"})
    receipt = {
        "network": NETWORK, "program_id": str(program_id), "advertiser_id": str(advertiser_id),
        "endpoint": APPLY_ENDPOINT, "request_body": body, "http_status": resp.status,
        "submitted_at_utc": b.utc(), "write_actions": 1, "secret_printed": False,
    }
    if resp.status not in (200, 201):
        receipt.update(status="failed", body_excerpt=resp.excerpt())
        raise RuntimeError(f"partnerize_campaign_request_http:{resp.status}:{resp.excerpt(160)}")
    doc = resp.json() if resp.text.strip() else {}
    receipt.update(provider_response=doc, success_count=doc.get("success_count"),
                   failure_count=doc.get("failure_count"))
    if int(doc.get("failure_count") or 0) > 0 and not int(doc.get("success_count") or 0):
        receipt.update(status="rejected_by_provider", confirmation_state="rejected")
        return receipt
    after = relationship(program_id, transport)
    receipt["provider_readback"] = after
    if after["present"]:
        receipt.update(status="SUBMITTED_VERIFIED", confirmation_state=after["state"])
    else:
        receipt.update(status="SUBMISSION_UNVERIFIED", confirmation_state="unconfirmed")
    return receipt


def join_adapter(config: dict, candidate: dict) -> dict:
    """affiliate_auto_apply.JOIN_ADAPTERS contract: (config, candidate) -> receipt."""
    return apply(candidate["program_id"], b.RequestsTransport(),
                 advertiser_id=candidate.get("advertiser_id") or candidate.get("program_id"))
