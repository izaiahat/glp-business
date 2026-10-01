#!/usr/bin/env python3
"""CJ Affiliate publisher adapter (PAT auth, requestor-cid 8010186).

readback      GET advertiser-lookup.api.cj.com/v2/advertiser-lookup   [live-proven]
capture_terms same endpoint, per advertiser: the record carries the commercial
              program terms CJ exposes to the PAT (actions/commissions, network
              rank, relationship, performance incentives). CJ's fuller "Program
              Terms API" spec sits behind the authenticated developer portal —
              recorded as a finding, not guessed.
tracking_link link-search.api.cj.com/v2/link-search (proven script) — lookup, not
              deterministic: CJ mints the click id per link record.
apply         NOT SUPPORTED. CJ documents Advertiser Lookup as the way to "find
              advertisers you may be missing so you can log in and apply" — the
              application itself is a portal action.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from . import _base as b

NETWORK = "cj"
DISPLAY = "CJ Affiliate"
LOOKUP = "https://advertiser-lookup.api.cj.com/v2/advertiser-lookup"
LINK_SEARCH = "https://link-search.api.cj.com/v2/link-search"
# CJ's link-search keys on the PROPERTY (website) id, not the requestor cid —
# passing the cid returns "Website id specified does not match your account".
# 101822575 is our proven property id (see reports/glp-clinic-scout/
# affiliate-reconciliation-20260802/72-cj-trt-nation-link-search.json).
WEBSITE_ID = "101822575"
DOCS = [
    "https://docs.cj.com/docs/finding-your-path",
    "https://developers.cj.com/",
    "https://junction.cj.com/article/cj-affiliates-apis-5-things-you-should-know",
]
CAPABILITIES = {"readback": "api", "capture_terms": "api",
                "tracking_link": "api", "apply": "none"}
APPLY_FINDING = {
    "reason": "CJ's publisher API suite (Advertiser Lookup, Link Search, Commission Detail, Program "
              "Terms, Product/Item, Automated Offer Feed) is read-only for relationship state. CJ's own "
              "documentation frames Advertiser Lookup as the way to 'find advertisers you may be missing "
              "so you can log in and apply to join other programs' — the join is a portal action.",
    "citations": ["https://docs.cj.com/docs/finding-your-path",
                  "https://junction.cj.com/article/cj-affiliates-apis-5-things-you-should-know"],
    "portal_route": "members.cj.com authenticated portal (scripts/cj_canary_apply.py lane)",
}


def _secret() -> tuple[str, str, str]:
    sec = b.secret_json("affiliate-marketplaces", "cj", "api.json")
    token = str(sec.get("personal_access_token") or "").strip()
    cid = str(sec.get("requestor_cid") or "").strip()
    website = str(sec.get("website_id") or sec.get("property_id") or WEBSITE_ID).strip()
    return token, cid, website


def _rows(root: ET.Element) -> list[dict]:
    def text(node, name):
        found = node.find(name)
        return (found.text or "").strip() if found is not None else ""

    out = []
    for adv in root.findall(".//advertiser"):
        # CJ's own taxonomy, e.g. <primary-category><parent>Clothing/Apparel</parent>
        # <child>Malls</child></primary-category>. Dropping it left 2,860
        # advertisers with a name and a URL and nothing else to classify on, so
        # every one of them read as a metadata-poor unknown.
        cat = adv.find("primary-category")
        categories = [v for v in ([cat.findtext("parent") if cat is not None else None,
                                   cat.findtext("child") if cat is not None else None])
                      if v and v.strip()]
        out.append({
            "program_id": text(adv, "advertiser-id"), "name": text(adv, "advertiser-name"),
            "relationship": text(adv, "relationship-status") or "unknown",
            "account_status": text(adv, "account-status"),
            "currency": text(adv, "currency"), "network_rank": text(adv, "network-rank"),
            "categories": categories,
            "language": text(adv, "language"),
            # CJ's advertiser-lookup publishes NO per-advertiser country on this
            # surface (verified against the raw XML 2026-08-06). Geo therefore
            # stays unverifiable here and the fail-closed rule holds — a named
            # ceiling, not a silent drop. Advertiser country is visible on the
            # members.cj.com portal, which is where that gap gets closed.
            "region": "",
            "url": text(adv, "program-url"),
            "actions": [{"name": (a.findtext("name") or "").strip(),
                         "type": (a.findtext("type") or "").strip(),
                         "commission": " ".join(
                             f"{(c.findtext('default') or '').strip()}"
                             for c in a.findall(".//commission"))}
                        for a in adv.findall(".//action")],
        })
    return out


def _lookup(transport, params: dict):
    token, cid, _ = _secret()
    return transport.request("GET", LOOKUP, params={"requestor-cid": cid, **params},
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})


# CJ's advertiser-lookup takes a relationship keyword in `advertiser-ids`.
# Sweeping only "joined" answers "what do we already have" and never "what
# could we apply to" — which is the question the application lane asks. The
# account has 8 joined advertisers and 2,860 notjoined ones (probed
# 2026-08-06); enumerating one bucket hid the whole applyable catalog.
RELATIONSHIPS = ("joined", "notjoined")


def _sweep(transport, relationship: str, max_pages: int) -> tuple[list[dict], str | None, list[dict]]:
    programs, blockers, total = [], [], None
    for page in range(1, max_pages + 1):
        resp = _lookup(transport, {"advertiser-ids": relationship,
                                   "records-per-page": "100", "page-number": str(page)})
        if not resp.ok:
            blockers.append({"relationship": relationship, "page": page,
                             "http_status": resp.status,
                             "error_class": "cj_advertiser_lookup_http",
                             "body_excerpt": resp.excerpt()})
            break
        root = ET.fromstring(resp.body)
        box = root.find(".//advertisers")
        if total is None and box is not None:
            total = box.get("total-matched")
        got = _rows(root)
        programs.extend(got)
        if not got or (total and len(programs) >= int(total)):
            break
    return programs, total, blockers


def readback(transport, relationships: tuple = RELATIONSHIPS, max_pages: int = 60) -> dict:
    """Every relationship bucket, fully paginated.

    `max_pages` defaults to 60 (6,000 advertisers at 100/page) so the notjoined
    catalog is not silently truncated; the loop still exits on `total-matched`.
    """
    if isinstance(relationships, str):        # back-compat with the old kwarg
        relationships = (relationships,)
    programs, blockers, totals, incomplete = [], [], {}, []
    for rel in relationships:
        got, total, errs = _sweep(transport, rel, max_pages)
        programs.extend(got)
        blockers.extend(errs)
        totals[rel] = total
        if total and len(got) < int(total):
            incomplete.append({"relationship": rel, "fetched": len(got), "total_matched": total})
    return b.readback_payload(
        DISPLAY, "BLOCKED" if blockers and not programs else "PASS", programs,
        {"publisher_cid": _secret()[1], "total_matched": totals,
         "relationships_swept": list(relationships), "blockers": blockers,
         "incomplete_sweeps": incomplete,
         "endpoint": f"GET {LOOKUP}?advertiser-ids=<relationship>",
         "note": "joined AND notjoined are both swept and fully paginated; pending applications are not "
                 "exposed by this API (portal readback owns pending visibility)."})


def capture_terms(program_id, transport, terms_dir=None) -> dict:
    resp = _lookup(transport, {"advertiser-ids": str(program_id), "records-per-page": "100"})
    if not resp.ok:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "cj_advertiser_lookup_http",
                "body_excerpt": resp.excerpt()}
    rows = _rows(ET.fromstring(resp.body))
    row = next((r for r in rows if r["program_id"] == str(program_id)), None)
    if not row:
        return {"network": NETWORK, "program_id": str(program_id), "captured": False,
                "http_status": resp.status, "error_class": "cj_advertiser_not_returned"}
    actions = " ".join(f"Action {a['name']} ({a['type']}) commission {a['commission']}."
                       for a in row["actions"])
    text = (f"Advertiser: {row['name']} (id {row['program_id']}). "
            f"Relationship status: {row['relationship']}. Account status: {row['account_status']}. "
            f"Currency: {row['currency']}. Network rank: {row['network_rank']}. {actions}")
    out = b.save_terms(NETWORK, program_id, text,
                       sources=[{"kind": "cj_advertiser_lookup_api",
                                 "endpoint": f"GET {LOOKUP}?advertiser-ids={program_id}",
                                 "chars": len(text)}],
                       extra={"program_name": row["name"],
                              "full_program_terms_api_captured": False,
                              "full_program_terms_note": "CJ's Program Terms API spec is published behind the "
                                                         "authenticated developer portal; only the "
                                                         "advertiser-lookup commercial record is API-visible "
                                                         "with the stored PAT.",
                              "provider_record": row},
                       terms_dir=terms_dir)
    out["captured"] = True
    return out


def link_search(program_id, transport, max_pages: int = 5) -> dict:
    token, cid, website = _secret()
    links = []
    for page in range(1, max_pages + 1):
        resp = transport.request("GET", LINK_SEARCH,
                                 params={"website-id": website, "advertiser-ids": str(program_id),
                                         "records-per-page": "100", "page-number": str(page)},
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
        if not resp.ok:
            return {"ok": False, "http_status": resp.status, "body_excerpt": resp.excerpt()}
        root = ET.fromstring(resp.body)
        found = root.findall(".//link")
        for node in found:
            links.append({"link_id": (node.findtext("link-id") or "").strip(),
                          "link_name": (node.findtext("link-name") or "").strip(),
                          "link_type": (node.findtext("link-type") or "").strip(),
                          "clickUrl": (node.findtext("clickUrl") or "").strip(),
                          "destination": (node.findtext("destination") or "").strip()})
        if not found:
            break
    return {"ok": True, "advertiser_id": str(program_id), "link_count": len(links), "links": links}


def tracking_link(program: dict, subid: str = "site") -> dict:
    pid = str(program.get("program_id") or program.get("id") or "")
    if not pid:
        return b.link_unsupported("cj requires advertiser id")
    return b.link_lookup(
        ["python3", "scripts/marketplace_adapter_cli.py", "--network", "cj",
         "--links", "--program-id", pid],
        f"CJ mints per-link click ids; GET {LINK_SEARCH} returns the advertiser's official links "
        "(pick the default text link and append sid= for subid tracking)")


def apply(program_id, transport=None, **kwargs):
    raise b.NotSupported(NETWORK, "apply", APPLY_FINDING["reason"], APPLY_FINDING["citations"])
