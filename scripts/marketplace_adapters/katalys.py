#!/usr/bin/env python3
"""Katalys / RevOffers adapter (aff_id 13031).

Katalys publishes an API for REPORTING ONLY ("run Statistics and Conversions
reports against your data housed with the Katalys system"). Offer enumeration,
link creation and program applications live in the KMP platform UI, which the
proven Playwright lane drives.

readback      NOT AVAILABLE via API — reporting endpoints only.
capture_terms NOT AVAILABLE via API.
tracking_link documented grammar is track.revoffers.com/aff_c?offer_id=&aff_id=,
              BUT Katalys explicitly warns that "generating your own tracking
              links might result in links that do not work" and directs
              publishers to the Create Link modal. We therefore keep the proven
              generator as the resolver route rather than claiming determinism.
apply         NOT SUPPORTED via API.
"""
from __future__ import annotations

from . import _base as b

NETWORK = "katalys"
DISPLAY = "Katalys / RevOffers"
AFF_ID = "13031"
TRACK = "https://track.revoffers.com/aff_c"
DOCS = [
    "https://kb.katalys.com/kb/api-access",
    "https://kb.katalys.com/kb/tracking-url-parameters",
]
CAPABILITIES = {"readback": "portal", "capture_terms": "portal",
                "tracking_link": "portal", "apply": "none"}
API_FINDING = {
    "reason": "Katalys' documented API surface is reporting-only (Statistics Reports, Conversion Reports) "
              "and requires a KMP-issued API key. No offer-enumeration, link-generation or application "
              "endpoint is published, so readback/terms/apply cannot be implemented against a citation.",
    "citations": ["https://kb.katalys.com/kb/api-access"],
    "portal_route": "scripts/katalys_generate_tracking_links.py (proven Playwright lane, "
                    "traffic source 'GLP Clinic Scout Site')",
}
LINK_FINDING = {
    "reason": "Katalys documents the aff_c grammar (offer_id, aff_id, url_id/url, sub1..sub5) but warns "
              "that self-generated links may not work and directs publishers to the Create Link modal. "
              "Claiming deterministic construction here would risk uncredited clicks.",
    "citations": ["https://kb.katalys.com/kb/tracking-url-parameters"],
}


# The taxonomy terms the Playwright capture returns per offer. Katalys carries
# the two fields the application filter needs — they were simply being dropped
# on the floor by the normalizer (verification audit 2026-08-06).
VERTICAL_TERM = "vertical"
COUNTRY_TERM = "country"
# Relationship states that mean "there is nothing left to apply for".
ACTIONED = {"approved", "pending", "invited", "rejected"}


def normalize_capture(capture: dict) -> dict:
    """Raw `katalys_marketplace_relationship_inventory_v1` -> standard inventory.

    THREE things this fixes, each of which alone zeroed Katalys as an
    application source (operator verification 2026-08-06):

      1. EVERY offer is emitted, not just the 93 that already have a
         relationship. The 312 `not_related` offers ARE the unapplied supply —
         dropping them left the applyable pool at zero by construction.
      2. `vertical` tags become `categories`. Katalys tags 46 offers
         "Weight Loss" and 65 "Rx"; the normalizer discarded the taxonomy and
         left the filter to guess from a brand name.
      3. `country` tags become `region`. All 405 offers are tagged "United
         States of America", so a blank region made every one of them fail the
         fail-closed geo gate as `region_unverified` — including "Embody GLP1",
         "SkinnyRx" and "TMates GLP1 Prescription Weight Loss".
    """
    offers = capture.get("offers") or []
    programs, counts = [], {}
    for offer in offers:
        tags = offer.get("tags") or []
        verticals, countries = [], []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            term, name = str(tag.get("taxonomyTerm") or ""), str(tag.get("name") or "")
            if term == VERTICAL_TERM and name:
                verticals.append(name)
            elif term == COUNTRY_TERM and name:
                countries.append(name)
        statuses = [str((rel or {}).get("status") or "").lower()
                    for rel in (offer.get("relationships") or [])]
        statuses = [s for s in statuses if s]
        relationship = "+".join(sorted(set(statuses))) if statuses else "not_related"
        counts[relationship] = counts.get(relationship, 0) + 1
        programs.append({
            "program_id": str(offer.get("offerId") or ""),
            "name": str(offer.get("offerName") or ""),
            "relationship": relationship,
            "categories": sorted(set(verticals)),
            # One country -> that IS the region. Multi-country offers keep the
            # list in `countries` and let the US signal stand only when the US
            # is among them.
            "region": (countries[0] if len(countries) == 1 else
                       ("US" if any("united states" in c.lower() for c in countries) else "")),
            "countries": sorted(set(countries)),
            "description": "",
            "currency": "",
            "url": str(offer.get("offerUrl") or offer.get("url") or ""),
        })
    total = int(capture.get("marketplace_total") or len(offers))
    unapplied = [p for p in programs if p["relationship"] not in ACTIONED]
    return b.readback_payload(
        DISPLAY, "PASS" if len(programs) == total else "PARTIAL", programs,
        {"marketplace_total": total, "normalized_offer_count": len(programs),
         "relationship_counts": counts, "unapplied_count": len(unapplied),
         "network_region": "United States of America",
         "source_capture_schema": str(capture.get("schema") or ""),
         "note": "ALL marketplace offers including not_related (the unapplied supply); "
                 "vertical tags -> categories, country tags -> region."})


def readback(transport=None):
    raise b.NotSupported(NETWORK, "readback", API_FINDING["reason"], API_FINDING["citations"])


def capture_terms(program_id, transport=None, terms_dir=None):
    raise b.NotSupported(NETWORK, "capture_terms", API_FINDING["reason"], API_FINDING["citations"])


def documented_link(program: dict, subid: str = "site") -> str:
    """The documented grammar, exposed for validation/comparison — never returned
    as `resolved` (see LINK_FINDING)."""
    return f"{TRACK}?" + b.qs({"offer_id": str(program.get("program_id") or program.get("offer_id") or ""),
                               "aff_id": AFF_ID, "sub1": subid})


def tracking_link(program: dict, subid: str = "site") -> dict:
    return b.link_lookup(
        ["/home/ubuntu/business/.venv-affiliate-blitz/bin/python",
         "scripts/katalys_generate_tracking_links.py"],
        "katalys links are minted per-program by the proven generator (track.revoffers.com aff_c pattern, "
        f"aff_id {AFF_ID}); provider docs warn against self-generated links")


def apply(program_id, transport=None, **kwargs):
    raise b.NotSupported(NETWORK, "apply", API_FINDING["reason"], API_FINDING["citations"])


# Reporting is additive: the portal/application/link contracts above are unchanged.
REPORT_API = "https://api.katalys.com/v1/report/"
CONVERSION_FIELDS = ["seq", "order_time", "conversion_status", "aff_id", "offer_id", "payout"]


class SharedReportingLimiter:
    """Cross-process account limiter; conservative 2.1s spacing (<30/min).

    Hold flock through response so a 429 cooldown is visible before any other
    cooperating reader can start. No auto-retry; callers retain the first read.
    Other consumers of this key must use this same state path.
    """
    def __init__(self, state_path=None, *, clock=None, sleep=None):
        import time
        from pathlib import Path
        self.path = Path(state_path) if state_path else b.SECRETS / "katalys/reporting-rate-limit.json"
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep

    def request(self, transport, method, url, **kwargs):
        import fcntl
        import json
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            text = state.read()
            not_before = float(json.loads(text)["not_before"]) if text else 0.0
            self.sleep(max(0.0, not_before - self.clock())) if not_before > self.clock() else None

            def save(when):
                state.seek(0)
                state.truncate()
                json.dump({"not_before": when}, state)
                state.flush()
                os.fsync(state.fileno())

            # Reserve before I/O: transport failures do not consume an unpaced retry.
            save(self.clock() + 2.1)
            response = transport.request(method, url, **kwargs)
            if response.status == 429:
                import math
                from email.utils import parsedate_to_datetime
                now = self.clock()
                cooldown = 60.0  # retain the conservative shared fallback
                retry_after = next((v for k, v in response.headers.items()
                                    if k.casefold() == 'retry-after'), None)
                if retry_after is not None:
                    try:
                        delay = (float(retry_after) if str(retry_after).strip().isdigit()
                                 else parsedate_to_datetime(str(retry_after)).timestamp() - now)
                        if math.isfinite(delay):
                            cooldown = max(cooldown, delay)
                    except (ValueError, TypeError, OverflowError):
                        pass
                save(now + cooldown)
            return response


def _report(kind, start_date, end_date, transport, *, profile_id,
            timezone="America/Los_Angeles", currency=None, currency_evidence=None, limiter=None):
    """Fixed-host, allowlisted reports. Never return provider errors or nested data."""
    import datetime as dt
    import hashlib
    import re
    import uuid
    from decimal import Decimal, InvalidOperation
    from zoneinfo import ZoneInfo

    if transport is None:
        raise ValueError("reporting requires an injected transport")
    if kind not in {"conversions", "stats"}:
        raise ValueError("unsupported report")
    if not isinstance(profile_id, str) or str(uuid.UUID(profile_id)) != profile_id:
        raise ValueError("canonical profile UUID required")
    if currency is not None and (not re.fullmatch(r"[A-Z]{3}", currency) or not currency_evidence):
        raise ValueError("currency requires uppercase ISO code and account evidence")
    for date in [start_date, end_date]:
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("ISO calendar dates required")
    zone = ZoneInfo(timezone)
    start = dt.datetime.combine(dt.date.fromisoformat(start_date), dt.time(), zone)
    end = dt.datetime.combine(dt.date.fromisoformat(end_date) + dt.timedelta(days=1), dt.time(), zone)
    if end <= start:
        raise ValueError("end date precedes start date")
    limits = [str(int(start.timestamp() * 1000)), str(int(end.timestamp() * 1000) - 1)]
    fields = CONVERSION_FIELDS if kind == "conversions" else ["aff_id", "conversions", "payout"]
    query = {"dimensions": list(fields), "filters": [
        {"field": "time", "type": "range", "values": limits},
        {"field": "org_id", "type": "include", "values": [profile_id]}]}
    if kind == "stats":
        # Fixed-offset API does not express DST transitions. Reject spanning one.
        if start.utcoffset() != (end - dt.timedelta(milliseconds=1)).utcoffset():
            raise ValueError("statistics window crosses a UTC offset change; split window")
        offset = start.strftime("%z")
        query["filters"].append({"field": "timezone", "type": "include", "values": [offset[:3] + ":" + offset[3:]]})
    result = {"network": NETWORK, "report": kind, "status": "BLOCKED", "profile_id": profile_id,
              "start_date": start_date, "end_date": end_date, "timezone": timezone,
              "currency": currency, "currency_evidence": currency_evidence, "paid_cash": None,
              "financial_kind": "reported_commission_not_cash", "rows": [], "row_count": None,
              "query": query, "http_status": None, "completeness": "UNKNOWN",
              "classification": "TEST_REAL_UNKNOWN", "retrieved_at": b.utc()}
    result["as_of"] = result["retrieved_at"]
    result["requested_end_utc"] = (end - dt.timedelta(milliseconds=1)).astimezone(dt.timezone.utc).isoformat()
    result["period_state"] = ("CLOSED" if dt.datetime.fromisoformat(result["as_of"].replace("Z", "+00:00")) >= end
                              else "OPEN_PARTIAL_DAY")
    try:
        # Requests normally follows redirects. Fail before forwarding a custom
        # Authorization scheme to any redirect destination (including same host).
        if isinstance(transport, b.RequestsTransport):
            transport._sess().max_redirects = 0
        response = (limiter or SharedReportingLimiter()).request(transport, "POST", REPORT_API + kind, headers={
            "Authorization": "apikey " + b.secret_text("katalys", "reporting_api_key"),
            "Content-Type": "application/json"}, json_body=query)
    except Exception:
        result["reason"] = "transport_error"
        return result
    result.update(http_status=response.status, response_sha256=hashlib.sha256(response.body).hexdigest())
    if response.status != 200:
        result["reason"] = "http_error"
        return result
    try:
        import json
        payload = json.loads(response.text, parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError("missing rows")
        known_envelope = {"rows", "success", "generatedAt", "took", "error", "errors", "warning", "warnings",
                          "has_more", "hasMore", "next_page", "next", "cursor", "truncated", "total", "total_count", "totalCount", "rowCount"}
        if set(payload) - known_envelope:
            raise ValueError("unrecognized completeness metadata")
        if "success" in payload:
            if payload["success"] is not True:
                raise ValueError("provider rejected report")
            result["provider_success"] = True
        if any(payload.get(x) for x in ["error", "errors", "warning", "warnings", "has_more", "hasMore", "next_page", "next", "cursor", "truncated"]):
            raise ValueError("incomplete response")
        for total_key in ["total", "total_count", "totalCount", "rowCount"]:
            if total_key in payload:
                if not re.fullmatch(r"\d+", str(payload[total_key])):
                    raise ValueError("count mismatch")
                if int(payload[total_key]) != len(payload["rows"]):
                    result.update(reason="katalys_rowcount_rows_mismatch", declared_row_count=int(payload[total_key]),
                                  returned_row_count=len(payload["rows"]))
                    raise ValueError("count mismatch")
        rows, identities = [], set()
        for raw in payload["rows"]:
            if not isinstance(raw, dict) or any(field not in raw for field in fields):
                raise ValueError("row schema")
            row = {}
            for field in fields:
                value = raw[field]
                if value is None:
                    row[field] = None
                    continue
                if isinstance(value, (bool, dict, list)):
                    raise ValueError("non-scalar value")
                if field == "seq":
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value)):
                        raise ValueError("invalid sequence")
                    row[field] = str(value)
                elif field == "payout":
                    number = Decimal(str(value))
                    if not number.is_finite():
                        raise ValueError("invalid amount")
                    row[field] = str(number)
                else:
                    if not re.fullmatch(r"-?\d+", str(value)):
                        raise ValueError("invalid numeric field")
                    row[field] = str(value) if field in {"aff_id", "offer_id"} else int(value)
            if kind == "conversions":
                if row["seq"] is None or row["seq"] in identities:
                    raise ValueError("missing or duplicate conversion")
                identities.add(row["seq"])
                if row["order_time"] is None or not int(limits[0]) <= row["order_time"] <= int(limits[1]):
                    raise ValueError("out of period")
                if row["aff_id"] is None or row["offer_id"] is None:
                    raise ValueError("missing identity")
            else:
                if row["aff_id"] is None or row["aff_id"] in identities:
                    raise ValueError("missing or duplicate traffic source")
                identities.add(row["aff_id"])
            rows.append(row)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        result.update(status="PARTIAL", reason=result.get("reason", "invalid_or_incomplete_response"),
                      completeness="INCOMPLETE")
        return result
    result.update(status="PASS", rows=rows, row_count=len(rows), completeness="COMPLETE")
    return result


def conversion_report(start_date, end_date, transport=None, *, profile_id,
                      timezone="America/Los_Angeles", currency=None, currency_evidence=None, limiter=None):
    """Read all conversion states; commission status is not paid-cash evidence."""
    return _report("conversions", start_date, end_date, transport, profile_id=profile_id,
                   timezone=timezone, currency=currency, currency_evidence=currency_evidence, limiter=limiter)


def statistics_report(start_date, end_date, transport=None, *, profile_id,
                      timezone="America/Los_Angeles", currency=None, currency_evidence=None, limiter=None):
    """Low-cardinality traffic-source commission report; excludes customer dimensions."""
    return _report("stats", start_date, end_date, transport, profile_id=profile_id,
                   timezone=timezone, currency=currency, currency_evidence=currency_evidence, limiter=limiter)


# Kept separate: CAPABILITIES is an exact four-verb public contract.
REPORTING_CAPABILITIES = {"conversions": "api", "commission_statistics": "api", "paid_cash": "none"}
