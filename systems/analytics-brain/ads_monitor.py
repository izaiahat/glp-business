#!/usr/bin/env python3
"""Ads readback + stop-loss evaluation for the ``glp/analytics-brain`` ``ads`` phase.

This is NOT a new machine. It is the ads leg of the existing analytics brain: the
cadence wrapper (``scripts/glp_analytics_brain_cadence.py``) calls ``collect()``
when the n8n workflow ``glp/analytics-brain`` (JY7iv9pljAu1R3TI) fires with
``phase=ads``, and the digest/alert leaves through the same Discord path every
other analytics-brain failure already uses.

READ-ONLY BY CONSTRUCTION. Every outbound HTTP call goes through ``_http`` which
refuses anything that is not a GET or the one whitelisted Google Ads *search*
POST. Budget and bid mutation stay operator-gated: this module cannot express a
mutation, and ``tests/test_glp_analytics_brain_ads_phase.py`` asserts that.

Sources, and why each one is read this way:

* **Google Ads** — the REST ``googleAds:search`` endpoint with the box's own
  OAuth refresh token (``~/.hermes/secrets/google-ads/``). Cogny's MCP holds the
  same account, but MCP is an in-session agent transport; a bridge job running
  under cron has no MCP. Reading natively is what makes the machine durable.
* **Meta** — the Graph API with the system-user token
  (``~/.hermes/secrets/meta/glp-clinic-scout-system-user-token.txt``). The token
  is never printed, logged, or written into a receipt.
* **Bing / Microsoft Advertising** — DECLARED UI-READBACK-ONLY. There is no
  Microsoft Advertising account and no developer token, so there is nothing to
  call. The monitor says so in every digest rather than reporting a silent zero
  that could be mistaken for "read it, found nothing".
* **The /go click join** — PostHog (``cs_click_id`` canary) plus the three D1
  ledgers: ``affiliate_click_map`` (what ``/go`` mints), ``analytics_click_ledger_v1``
  (the brain's canonical click fact) and ``analytics_revenue_ledger_v1`` (what the
  postback receiver accepted).

A zero-spend readback is still a readback: with every campaign paused this module
returns a complete, successful payload whose spend happens to be 0. It never
reports "no data" for a source it actually reached.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

BUSINESS = Path(__file__).resolve().parents[2]
CONTROL = BUSINESS / "ops/glp/analytics-brain-control.json"
DEFAULT_OUTPUT = BUSINESS / "reports/glp-clinic-scout/analytics/ads"

GOOGLE_ADS_VERSION = "v24"
GOOGLE_ADS_CUSTOMER_ID = "1177599039"
GOOGLE_ADS_LOGIN_CUSTOMER_ID = "2847481998"
GOOGLE_ADS_DEV_TOKEN = Path.home() / ".hermes/secrets/google-ads/developer-token.txt"
GOOGLE_ADS_OAUTH_TOKEN = Path.home() / ".hermes/secrets/google-ads/oauth-token.json"
GOOGLE_ADS_OAUTH_CLIENT = Path.home() / ".hermes/secrets/google-ads/oauth-client.json"

META_GRAPH_VERSION = "v21.0"
META_AD_ACCOUNT = "act_243816027637970"
META_PIXEL_ID = "586886932687286"
META_PAGE_ID = "1022556670932300"
META_TOKEN = Path.home() / ".hermes/secrets/meta/glp-clinic-scout-system-user-token.txt"

PRODUCTION_HOST = "glpclinicscout.com"

# Defaults; ops/glp/analytics-brain-control.json -> "ads" overrides any of them.
DEFAULT_ADS_CONTROL: dict[str, Any] = {
    "canary_dark_hours": 26,
    "go_ledger_dark_hours": 26,
    "meta_pixel_dark_hours": 48,
    "spend_window": "LAST_7_DAYS",
    "digest_hour_pt": 9,
    "bing_transport": "ui_readback_only",
}

READ_ONLY_POST_SUFFIX = f"/{GOOGLE_ADS_VERSION}/customers/{GOOGLE_ADS_CUSTOMER_ID}/googleAds:search"
# OAuth's refresh grant is a POST by protocol and mutates nothing in any ad
# account. It is allowlisted by exact host so that EVERY outbound request in this
# module goes through one guard — an exception carved out here is visible; a
# `requests.post` that quietly bypasses the guard is not.
OAUTH_TOKEN_HOSTS = frozenset({"oauth2.googleapis.com", "accounts.google.com",
                               "www.googleapis.com"})


class MutationRefused(RuntimeError):
    """Raised when anything tries to send a write through this module."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def stamp(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return None


def _age_hours(value: Any, now: dt.datetime | None = None) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return round(((now or utc_now()) - parsed).total_seconds() / 3600.0, 2)


def _assert_read_only(method: str, url: str) -> None:
    """The single choke point that makes 'observe and alert only' structural.

    A GET can be a mutation on a badly designed API, so the allowlist is by
    endpoint, not only by verb: the one POST this module may ever send is Google
    Ads' *search*. ``:mutate``, ``:mutateJob`` and every other write path is
    refused here rather than being merely 'not called anywhere'.
    """
    verb = method.upper()
    bare = url.split("?")[0]
    if "mutate" in bare.rsplit("/", 1)[-1].lower():
        raise MutationRefused(f"mutation endpoint refused: {bare}")
    if verb == "GET":
        return
    if verb == "POST" and bare.endswith(READ_ONLY_POST_SUFFIX):
        return
    if verb == "POST" and urllib.parse.urlparse(bare).hostname in OAUTH_TOKEN_HOSTS:
        return
    raise MutationRefused(f"{verb} refused by the ads monitor read-only guard: {bare}")


def _http(method: str, url: str, *, headers: dict[str, str] | None = None,
          payload: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
          timeout: int = 60) -> tuple[int, Any]:
    _assert_read_only(method, url)
    import requests  # imported late so the guard above always runs first

    response = requests.request(
        method.upper(), url, headers=headers or {},
        json=payload if payload is not None else None,
        data=data if data is not None else None, timeout=timeout,
    )
    try:
        body: Any = response.json()
    except ValueError:
        body = {"text_preview": response.text[:400]}
    return response.status_code, body


def load_control(path: Path | None = None) -> dict[str, Any]:
    control_path = path or CONTROL
    payload: dict[str, Any] = {}
    if control_path.is_file():
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    ads = dict(DEFAULT_ADS_CONTROL)
    ads.update(payload.get("ads") or {})
    return ads


# ---------------------------------------------------------------- Google Ads --

def _google_access_token() -> str:
    token_doc = json.loads(GOOGLE_ADS_OAUTH_TOKEN.read_text(encoding="utf-8"))
    client_doc = json.loads(GOOGLE_ADS_OAUTH_CLIENT.read_text(encoding="utf-8"))
    client = client_doc.get("installed") or client_doc.get("web") or client_doc
    for key in ("client_id", "client_secret", "token_uri"):
        if not token_doc.get(key) and client.get(key):
            token_doc[key] = client[key]
    status, body = _http("POST", str(token_doc["token_uri"]), data={
        "client_id": token_doc["client_id"],
        "client_secret": token_doc["client_secret"],
        "refresh_token": token_doc["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if status != 200:
        raise RuntimeError(f"google_ads_oauth_refresh_failed:{status}")
    access_token = (body or {}).get("access_token")
    if not access_token:
        raise RuntimeError("google_ads_oauth_no_access_token")
    return str(access_token)


def _gaql(query: str, access_token: str, developer_token: str) -> list[dict[str, Any]]:
    url = f"https://googleads.googleapis.com{READ_ONLY_POST_SUFFIX}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "login-customer-id": GOOGLE_ADS_LOGIN_CUSTOMER_ID,
        "Content-Type": "application/json",
        "User-Agent": "glp-analytics-brain-ads/1.0",
    }
    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        payload: dict[str, Any] = {"query": query}
        if page_token:
            payload["pageToken"] = page_token
        status, body = _http("POST", url, headers=headers, payload=payload)
        if status != 200:
            raise RuntimeError(f"gaql_http_{status}")
        rows.extend(body.get("results") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            return rows


def google_ads_readback(control: dict[str, Any]) -> dict[str, Any]:
    window = str(control.get("spend_window") or "LAST_7_DAYS")
    try:
        developer_token = GOOGLE_ADS_DEV_TOKEN.read_text(encoding="utf-8").strip()
        access_token = _google_access_token()
    except Exception as exc:  # noqa: BLE001
        return {"platform": "google", "state": "error", "transport": "google_ads_rest",
                "reason": str(exc)[:300]}

    try:
        campaigns = _gaql(
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, campaign_budget.amount_micros, "
            "metrics.cost_micros, metrics.clicks, metrics.impressions, "
            "metrics.conversions, metrics.all_conversions "
            f"FROM campaign WHERE segments.date DURING {window}",
            access_token, developer_token)
        lifetime = _gaql(
            "SELECT campaign.id, campaign.name, campaign.status, "
            "metrics.cost_micros, metrics.clicks, metrics.conversions FROM campaign",
            access_token, developer_token)
        ads = _gaql(
            "SELECT campaign.name, campaign.status, ad_group.name, ad_group_ad.ad.id, "
            "ad_group_ad.status, ad_group_ad.policy_summary.approval_status, "
            "ad_group_ad.policy_summary.review_status FROM ad_group_ad",
            access_token, developer_token)
        actions = _gaql(
            "SELECT conversion_action.id, conversion_action.name, conversion_action.status, "
            "conversion_action.type, conversion_action.primary_for_goal FROM conversion_action",
            access_token, developer_token)
    except Exception as exc:  # noqa: BLE001
        return {"platform": "google", "state": "error", "transport": "google_ads_rest",
                "reason": str(exc)[:300]}

    def metric(row: dict[str, Any], key: str) -> float:
        return float((row.get("metrics") or {}).get(key) or 0)

    window_cost = sum(metric(row, "costMicros") for row in campaigns) / 1_000_000
    window_clicks = int(sum(metric(row, "clicks") for row in campaigns))
    window_conversions = sum(metric(row, "conversions") for row in campaigns)
    lifetime_cost = sum(metric(row, "costMicros") for row in lifetime) / 1_000_000
    lifetime_clicks = int(sum(metric(row, "clicks") for row in lifetime))

    disapproved = [
        {
            "campaign": (row.get("campaign") or {}).get("name"),
            "campaign_status": (row.get("campaign") or {}).get("status"),
            "ad_group": (row.get("adGroup") or {}).get("name"),
            "ad_id": ((row.get("adGroupAd") or {}).get("ad") or {}).get("id"),
            "ad_status": (row.get("adGroupAd") or {}).get("status"),
            "approval_status": (((row.get("adGroupAd") or {}).get("policySummary") or {})
                                .get("approvalStatus")),
        }
        for row in ads
        if (((row.get("adGroupAd") or {}).get("policySummary") or {}).get("approvalStatus")
            in {"DISAPPROVED", "AREA_OF_INTEREST_ONLY"})
    ]
    limited = [
        ((row.get("adGroupAd") or {}).get("ad") or {}).get("id")
        for row in ads
        if (((row.get("adGroupAd") or {}).get("policySummary") or {}).get("approvalStatus")
            == "APPROVED_LIMITED")
    ]

    return {
        "platform": "google",
        "state": "healthy",
        "transport": "google_ads_rest",
        "customer_id": GOOGLE_ADS_CUSTOMER_ID,
        "login_customer_id": GOOGLE_ADS_LOGIN_CUSTOMER_ID,
        "window": window,
        "window_spend_usd": round(window_cost, 2),
        "window_clicks": window_clicks,
        "window_conversions": round(window_conversions, 2),
        "lifetime_spend_usd": round(lifetime_cost, 2),
        "lifetime_clicks": lifetime_clicks,
        "campaign_count": len(lifetime),
        "enabled_campaigns": [
            (row.get("campaign") or {}).get("name") for row in lifetime
            if (row.get("campaign") or {}).get("status") == "ENABLED"
        ],
        "ad_count": len(ads),
        "disapproved_ads": disapproved,
        "approved_limited_ad_ids": limited,
        "conversion_actions": [
            {
                "id": (row.get("conversionAction") or {}).get("id"),
                "name": (row.get("conversionAction") or {}).get("name"),
                "status": (row.get("conversionAction") or {}).get("status"),
                "type": (row.get("conversionAction") or {}).get("type"),
                "primary_for_goal": (row.get("conversionAction") or {}).get("primaryForGoal"),
            }
            for row in actions
        ],
        "read_at_utc": stamp(),
    }


# ---------------------------------------------------------------------- Meta --

def _graph(path: str, params: dict[str, str], token: str) -> Any:
    query = dict(params)
    query["access_token"] = token
    url = (f"https://graph.facebook.com/{META_GRAPH_VERSION}/{path}"
           f"?{urllib.parse.urlencode(query)}")
    status, body = _http("GET", url, timeout=45)
    if status != 200:
        # The token can appear in an echoed request URL; never let that reach a receipt.
        raise RuntimeError(f"graph_http_{status}:{str(body)[:160].replace(token, '[REDACTED]')}")
    return body


def meta_readback(control: dict[str, Any]) -> dict[str, Any]:
    try:
        token = META_TOKEN.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        return {"platform": "meta", "state": "error", "transport": "graph_system_user",
                "reason": f"token_unreadable:{type(exc).__name__}"}
    try:
        account = _graph(META_AD_ACCOUNT,
                         {"fields": "name,account_status,currency,amount_spent,disable_reason"},
                         token)
        insights = _graph(f"{META_AD_ACCOUNT}/insights",
                          {"level": "campaign", "date_preset": "last_7d",
                           "fields": "campaign_name,spend,impressions,clicks"}, token)
        campaigns = _graph(f"{META_AD_ACCOUNT}/campaigns",
                           {"fields": "name,status,effective_status,objective", "limit": "100"},
                           token)
        pixel = _graph(META_PIXEL_ID, {"fields": "name,last_fired_time,is_unavailable"}, token)
    except Exception as exc:  # noqa: BLE001
        return {"platform": "meta", "state": "error", "transport": "graph_system_user",
                "reason": str(exc)[:300]}

    rows = insights.get("data") or []
    spend = sum(float(row.get("spend") or 0) for row in rows)
    clicks = int(sum(float(row.get("clicks") or 0) for row in rows))
    campaign_rows = campaigns.get("data") or []
    pixel_age = _age_hours(pixel.get("last_fired_time"))
    return {
        "platform": "meta",
        "state": "healthy",
        "transport": "graph_system_user",
        "ad_account": META_AD_ACCOUNT,
        "account_status": account.get("account_status"),
        "window": "last_7d",
        "window_spend_usd": round(spend, 2),
        "window_clicks": clicks,
        "lifetime_spend_usd": round(float(account.get("amount_spent") or 0) / 100.0, 2),
        "campaign_count": len(campaign_rows),
        "active_campaigns": [row.get("name") for row in campaign_rows
                             if row.get("effective_status") == "ACTIVE"],
        "pixel_id": META_PIXEL_ID,
        "pixel_last_fired": pixel.get("last_fired_time"),
        "pixel_dark_hours": pixel_age,
        "pixel_dark_threshold_hours": control.get("meta_pixel_dark_hours"),
        "page_id": META_PAGE_ID,
        "read_at_utc": stamp(),
    }


# ---------------------------------------------------------------------- Bing --

def bing_readback(control: dict[str, Any]) -> dict[str, Any]:
    """Declared UI-readback-only. There is nothing to call, and that is the finding.

    Cogny holds Bing *Webmaster Tools*, not Microsoft Advertising; no Microsoft
    Advertising account exists and no developer token has been issued. Returning a
    zero here as if it were measured would be the exact class of error this whole
    machine exists to prevent, so the state is ``unavailable`` with the reason and
    the unblocking step attached.
    """
    return {
        "platform": "bing",
        "state": "unavailable",
        "transport": str(control.get("bing_transport") or "ui_readback_only"),
        "reason": "no_microsoft_advertising_account_and_no_developer_token",
        "measured": False,
        "unblock": ("create the Microsoft Advertising account on ops@, then apply for a "
                    "Microsoft Advertising API developer token; until both land, Bing "
                    "spend/clicks are read by a human in the UI, not by this machine"),
        "read_at_utc": stamp(),
    }


# ----------------------------------------------------------------- /go join ---

def _rails():
    path = BUSINESS / "ops/glp/seo_data_rails.py"
    spec = importlib.util.spec_from_file_location("ads_monitor_rails", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("seo_data_rails_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _d1_client():
    path = (BUSINESS
            / "reports/glp-clinic-scout/operator-national-wow-20260723/scripts"
              "/glp_observability_collect.py")
    spec = importlib.util.spec_from_file_location("ads_monitor_d1", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("d1_client_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    values = module.load_env(module.D1_SECRET)
    token = values.get("CLOUDFLARE_API_TOKEN") or values.get("CF_API_TOKEN")
    if not token:
        raise RuntimeError("d1_token_missing")
    return module.D1Client(token)


def click_join_readback(control: dict[str, Any]) -> dict[str, Any]:
    """The whole point of the machine: did the money click survive the join?

    Three independent sinks have to agree for a paid click to be worth anything —
    the browser event (PostHog, carrying ``cs_click_id``), the first-party ``/go``
    redirect (D1 ``affiliate_click_map``), and the network postback (D1
    ``analytics_revenue_ledger_v1``). Disagreement between them IS the finding, so
    each is reported separately and never averaged into one green number.
    """
    result: dict[str, Any] = {"read_at_utc": stamp()}

    try:
        posthog = _rails().PostHog()
        canary = posthog.hogql(
            "SELECT count(), countIf(properties.cs_click_id IS NOT NULL "
            "AND properties.cs_click_id != ''), max(timestamp) FROM events "
            "WHERE event = 'affiliate_click' AND timestamp > now() - INTERVAL 30 DAY "
            f"AND properties.$host = '{PRODUCTION_HOST}'"
        )
        if not canary.get("ok"):
            raise RuntimeError(str(canary.get("error") or "posthog_canary_failed"))
        row = (canary.get("rows") or [[0, 0, None]])[0]
        paid = posthog.hogql(
            "SELECT countIf(properties.gclid IS NOT NULL AND properties.gclid != ''), "
            "countIf(properties.fbclid IS NOT NULL AND properties.fbclid != ''), "
            "countIf(properties.msclkid IS NOT NULL AND properties.msclkid != '') "
            "FROM events WHERE timestamp > now() - INTERVAL 7 DAY "
            f"AND properties.$host = '{PRODUCTION_HOST}'"
        )
        paid_row = (paid.get("rows") or [[0, 0, 0]])[0] if paid.get("ok") else [None, None, None]
        result["posthog"] = {
            "state": "healthy",
            "project": "508172",
            "affiliate_click_30d": int(row[0] or 0),
            "with_cs_click_id_30d": int(row[1] or 0),
            "last_affiliate_click": str(row[2]) if row[2] else None,
            "canary_dark_hours": _age_hours(row[2]),
            "canary_dark_threshold_hours": control.get("canary_dark_hours"),
            "paid_click_ids_7d": {"gclid": paid_row[0], "fbclid": paid_row[1],
                                  "msclkid": paid_row[2]},
        }
    except Exception as exc:  # noqa: BLE001
        result["posthog"] = {"state": "error", "reason": str(exc)[:300]}

    try:
        client = _d1_client()
        go_rows = client.query(
            "SELECT COUNT(*) AS n, MAX(created_at) AS latest FROM affiliate_click_map")
        ledger_rows = client.query(
            "SELECT COUNT(*) AS n, MAX(occurred_at) AS latest FROM analytics_click_ledger_v1")
        revenue_rows = client.query(
            "SELECT COUNT(*) AS n, MAX(occurred_at) AS latest, "
            "SUM(COALESCE(commission_minor,0)) AS commission_minor "
            "FROM analytics_revenue_ledger_v1 WHERE network <> '__receiver_selftest__'")
        selftest_rows = client.query(
            "SELECT COUNT(*) AS n, MAX(occurred_at) AS latest FROM analytics_revenue_ledger_v1 "
            "WHERE network = '__receiver_selftest__'")
        unjoined = client.query(
            "SELECT COUNT(*) AS n FROM analytics_revenue_ledger_v1 r "
            "WHERE r.network <> '__receiver_selftest__' AND NOT EXISTS ("
            "SELECT 1 FROM affiliate_click_map c WHERE c.click_token = r.cs_click_id)")
        go = go_rows[0] if go_rows else {}
        result["d1"] = {
            "state": "healthy",
            "go_click_map_rows": int(go.get("n") or 0),
            "go_click_map_latest": go.get("latest"),
            "go_dark_hours": _age_hours(go.get("latest")),
            "go_dark_threshold_hours": control.get("go_ledger_dark_hours"),
            "canonical_click_ledger_rows": int((ledger_rows[0] if ledger_rows else {}).get("n") or 0),
            "canonical_click_ledger_latest": (ledger_rows[0] if ledger_rows else {}).get("latest"),
            "postback_rows": int((revenue_rows[0] if revenue_rows else {}).get("n") or 0),
            "postback_latest": (revenue_rows[0] if revenue_rows else {}).get("latest"),
            "postback_commission_minor": (revenue_rows[0] if revenue_rows else {}).get("commission_minor"),
            "postback_selftest_rows": int((selftest_rows[0] if selftest_rows else {}).get("n") or 0),
            "postback_unjoined_to_go_click": int((unjoined[0] if unjoined else {}).get("n") or 0),
        }
    except Exception as exc:  # noqa: BLE001
        result["d1"] = {"state": "error", "reason": str(exc)[:300]}

    return result


# --------------------------------------------------------------- evaluation --

def evaluate(readback: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    """Turn the readback into findings. Severity depends on whether money is moving.

    A tracking break while every campaign is paused is a *staging* defect: it goes
    in the digest, because paging the operator hourly about a machine that is not
    spending is the noise the daily-review rule exists to stop. The identical break
    with spend on the clock is an emergency, because every dollar is buying an
    unattributable click. One rule, two severities, decided by measured spend.
    """
    immediate: list[dict[str, Any]] = []
    digest: list[dict[str, Any]] = []

    google = readback.get("google") or {}
    meta = readback.get("meta") or {}
    join = readback.get("click_join") or {}
    posthog = join.get("posthog") or {}
    d1 = join.get("d1") or {}

    google_spend = float(google.get("window_spend_usd") or 0)
    meta_spend = float(meta.get("window_spend_usd") or 0)
    total_spend = google_spend + meta_spend
    spending = total_spend > 0
    # A platform we could not read is NOT a platform spending zero. Spend is only
    # "known" when every spending platform answered; the digest says which.
    unreadable = [name for name, block in (("google", google), ("meta", meta))
                  if (block or {}).get("state") != "healthy"]
    spend_known = not unreadable

    def emit(finding: dict[str, Any], *, urgent: bool) -> None:
        finding["severity"] = "IMMEDIATE" if urgent else "DIGEST"
        (immediate if urgent else digest).append(finding)

    # 1. Spend with no joined clicks — the stop-loss the operator actually asked for.
    joined_clicks = int(d1.get("go_click_map_rows") or 0)
    go_dark_hours = d1.get("go_dark_hours")
    for platform, spend, clicks in (("google", google_spend, google.get("window_clicks")),
                                    ("meta", meta_spend, meta.get("window_clicks"))):
        if spend > 0 and (go_dark_hours is None
                          or go_dark_hours > float(control.get("go_ledger_dark_hours") or 26)):
            emit({
                "id": "SPEND_WITHOUT_JOINED_CLICKS",
                "platform": platform,
                "detail": (f"{platform} spent ${spend:.2f} in the window with "
                           f"{clicks} ad clicks, and the /go click join has minted nothing "
                           f"in {go_dark_hours if go_dark_hours is not None else 'ever'} hours. "
                           "Every dollar is buying an unattributable click."),
            }, urgent=True)

    # 2. Disapprovals. Live campaign => immediate; staged campaign => digest item.
    for ad in google.get("disapproved_ads") or []:
        emit({
            "id": "AD_DISAPPROVED",
            "platform": "google",
            "detail": (f"{ad.get('approval_status')} — {ad.get('campaign')} / "
                       f"{ad.get('ad_group')} / ad {ad.get('ad_id')} "
                       f"(campaign {ad.get('campaign_status')})"),
        }, urgent=ad.get("campaign_status") == "ENABLED")

    # 3. cs_click_id canary. This is the tracking-break signal named in the brief.
    canary_hours = posthog.get("canary_dark_hours")
    canary_threshold = float(control.get("canary_dark_hours") or 26)
    if posthog.get("state") == "healthy" and (canary_hours is None or canary_hours > canary_threshold):
        emit({
            "id": "CS_CLICK_ID_CANARY_DARK",
            "platform": "site",
            "detail": (f"no affiliate_click carrying cs_click_id on {PRODUCTION_HOST} for "
                       f"{canary_hours if canary_hours is not None else 'more than 30 days'} hours "
                       f"(threshold {canary_threshold}h). Last seen "
                       f"{posthog.get('last_affiliate_click')}."),
        }, urgent=spending)

    # 4. The /go redirect ledger itself.
    if d1.get("state") == "healthy" and (go_dark_hours is None
                                         or go_dark_hours > float(control.get("go_ledger_dark_hours") or 26)):
        emit({
            "id": "GO_LEDGER_DARK",
            "platform": "site",
            "detail": (f"D1 affiliate_click_map holds {joined_clicks} rows, latest "
                       f"{d1.get('go_click_map_latest')} ({go_dark_hours}h old). The /go "
                       "redirect is the only first-party record a paid click leaves."),
        }, urgent=spending)

    # 5. Meta pixel. Conversion observation on Meta is impossible while this is dark.
    pixel_hours = meta.get("pixel_dark_hours")
    if pixel_hours is not None and pixel_hours > float(control.get("meta_pixel_dark_hours") or 48):
        emit({
            "id": "META_PIXEL_DARK",
            "platform": "meta",
            "detail": (f"pixel {meta.get('pixel_id')} last fired "
                       f"{meta.get('pixel_last_fired')} ({pixel_hours}h ago). Meta cannot "
                       "observe a conversion until it fires again."),
        }, urgent=meta_spend > 0)

    # 6. Canonical click ledger empty while the browser sink is not — a join defect.
    if (d1.get("state") == "healthy"
            and int(d1.get("canonical_click_ledger_rows") or 0) == 0
            and int(posthog.get("affiliate_click_30d") or 0) > 0):
        emit({
            "id": "CANONICAL_CLICK_LEDGER_EMPTY",
            "platform": "site",
            "detail": (f"PostHog measured {posthog.get('affiliate_click_30d')} affiliate_click "
                       "events in 30 days but analytics_click_ledger_v1 has 0 rows — the "
                       "brain's canonical click fact is not being written."),
        }, urgent=spending)

    # 7. Postbacks that cannot be joined back to a click.
    unjoined = int(d1.get("postback_unjoined_to_go_click") or 0)
    if unjoined > 0:
        emit({
            "id": "POSTBACK_UNJOINED",
            "platform": "network",
            "detail": (f"{unjoined} accepted postback row(s) carry a cs_click_id with no "
                       "matching /go click. Revenue cannot be attributed to a campaign."),
        }, urgent=False)

    # 8. A source that errored is a monitoring FAILURE, always immediate. A monitor
    #    that cannot read cannot prove the account is not burning money, so this one
    #    does not get to be quiet just because the number it failed to fetch was
    #    absent. The re-alert cooldown upstream stops a transient outage from paging
    #    hourly; an unchanged failure still surfaces in the daily digest.
    for name, block in (("google", google), ("meta", meta),
                        ("posthog", posthog), ("d1", d1)):
        if block.get("state") == "error":
            emit({"id": "SOURCE_READ_FAILED", "platform": name,
                  "detail": (f"{name} readback failed: {str(block.get('reason'))[:200]} — "
                             "spend cannot be confirmed while this source is dark")},
                 urgent=True)

    return {
        "spending": spending,
        "spend_known": spend_known,
        "unreadable_platforms": unreadable,
        "total_window_spend_usd": round(total_spend, 2),
        "immediate": immediate,
        "digest": digest,
        "finding_count": len(immediate) + len(digest),
    }


# ------------------------------------------------------------------- digest --

# Discord rejects a message body over 2000 characters with a bare HTTP 400 and no
# hint about which field was wrong. A 13-finding digest is 2494 characters, so the
# very first live ads run failed on exactly this. Every message this module
# produces is fitted to the budget below, with the dropped findings named by count
# and the on-disk receipt carrying the full list.
DISCORD_CONTENT_LIMIT = 2000
DISCORD_CONTENT_BUDGET = 1900


def _fit(header: list[str], body: list[str], footer: list[str], receipt: str | None) -> str:
    """Keep the header and footer whole; drop findings from the tail if the budget bites."""
    kept = list(body)
    while True:
        overflow = len(body) - len(kept)
        tail = list(footer)
        if overflow:
            tail = [f"…and {overflow} more finding(s) — full list in "
                    f"`{receipt or 'the run receipt'}`"] + tail
        content = "\n".join(header + kept + tail)
        if len(content) <= DISCORD_CONTENT_BUDGET or not kept:
            return content[:DISCORD_CONTENT_LIMIT]
        kept.pop()


def digest_content(payload: dict[str, Any]) -> str:
    readback = payload["readback"]
    findings = payload["findings"]
    google = readback.get("google") or {}
    meta = readback.get("meta") or {}
    bing = readback.get("bing") or {}
    join = readback.get("click_join") or {}
    posthog = join.get("posthog") or {}
    d1 = join.get("d1") or {}

    lines = [
        f"📊 **GLP ads monitor** — {payload['status']} · {payload['generated_at_utc']}",
        "",
        f"**Google** ({google.get('state')}) — ${google.get('window_spend_usd', 0)} / "
        f"{google.get('window_clicks', 0)} clicks / {google.get('window_conversions', 0)} conv "
        f"[{google.get('window')}] · {len(google.get('disapproved_ads') or [])} disapproved ad(s) · "
        f"{len(google.get('enabled_campaigns') or [])} enabled campaign(s)",
        f"**Meta** ({meta.get('state')}) — ${meta.get('window_spend_usd', 0)} / "
        f"{meta.get('window_clicks', 0)} clicks [last_7d] · pixel last fired "
        f"{meta.get('pixel_last_fired')} · {len(meta.get('active_campaigns') or [])} active campaign(s)",
        f"**Bing** ({bing.get('state')}) — {bing.get('reason')}; transport "
        f"`{bing.get('transport')}` — NOT measured by this machine",
        (f"**Spend confirmed across every platform this machine can read.**"
         if findings.get("spend_known", True)
         else f"⚠️ **Spend NOT confirmed** — unreadable: "
              f"{', '.join(findings.get('unreadable_platforms') or [])}"),
        f"**/go join** — PostHog affiliate_click 30d: {posthog.get('affiliate_click_30d')} "
        f"({posthog.get('with_cs_click_id_30d')} with cs_click_id, last "
        f"{posthog.get('last_affiliate_click')}) · D1 /go rows: {d1.get('go_click_map_rows')} "
        f"(latest {d1.get('go_click_map_latest')}) · canonical ledger rows: "
        f"{d1.get('canonical_click_ledger_rows')} · postbacks: {d1.get('postback_rows')}",
    ]
    body: list[str] = []
    if findings["immediate"]:
        body += ["", "🔴 **Immediate**"]
        body += [f"- `{item['id']}` {item['detail']}" for item in findings["immediate"]]
    if findings["digest"]:
        body += ["", "🟡 **For review**"]
        body += [f"- `{item['id']}` {item['detail']}" for item in findings["digest"]]
    if not findings["immediate"] and not findings["digest"]:
        body += ["", "✅ No findings."]
    footer = ["", "-# observe-and-alert only; budget and bid changes stay operator-gated. "
                  "source: glp/analytics-brain phase=ads"]
    return _fit(lines, body, footer, payload.get("receipt"))


def alert_content(payload: dict[str, Any]) -> str:
    findings = payload["findings"]
    header = [
        f"🔴 **GLP ads stop-loss** — {len(findings['immediate'])} immediate finding(s) · "
        f"${findings['total_window_spend_usd']} measured spend in window",
        "",
    ]
    body = [f"- `{item['id']}` [{item['platform']}] {item['detail']}"
            for item in findings["immediate"]]
    footer = ["", "-# observe-and-alert only; no budget or bid was changed. "
                  "source: glp/analytics-brain phase=ads"]
    return _fit(header, body, footer, payload.get("receipt"))


# ------------------------------------------------------------------ collect --

def collect(*, control_path: Path | None = None, output_dir: Path | None = None,
            fixture: Path | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    control = load_control(control_path)
    moment = now or utc_now()
    if fixture is not None:
        readback = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        readback = {
            "google": google_ads_readback(control),
            "meta": meta_readback(control),
            "bing": bing_readback(control),
            "click_join": click_join_readback(control),
        }
    findings = evaluate(readback, control)
    sources_read = sum(
        1 for block in (readback.get("google"), readback.get("meta"),
                        (readback.get("click_join") or {}).get("posthog"),
                        (readback.get("click_join") or {}).get("d1"))
        if (block or {}).get("state") == "healthy"
    )
    status = "PASS_ADS_READBACK" if sources_read else "FAIL_ADS_NO_SOURCE_READ"
    payload = {
        "schema": "glp-analytics-brain-ads-readback-v1",
        "generated_at_utc": stamp(moment),
        "status": status,
        "phase": "ads",
        "sources_read": sources_read,
        "sources_expected": 4,
        "bing_measured": False,
        "readback": readback,
        "findings": findings,
        "control": control,
        "mutations_attempted": 0,
        "credentials_printed": False,
    }
    # A zero-spend readback is still a readback: output_count counts SOURCES READ,
    # never dollars, so a paused account still writes a heartbeat.
    payload["output_count"] = sources_read
    target = output_dir or DEFAULT_OUTPUT
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{stamp(moment).replace(':', '').replace('-', '')}-ads.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["receipt"] = str(path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="glp/analytics-brain ads-phase readback")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixture", type=Path, help="offline readback JSON; no provider is called")
    parser.add_argument("--control", type=Path)
    args = parser.parse_args(argv)
    payload = collect(control_path=args.control, output_dir=args.output_dir, fixture=args.fixture)
    print(json.dumps({
        "status": payload["status"], "phase": "ads",
        "output_count": payload["output_count"],
        "immediate": len(payload["findings"]["immediate"]),
        "digest": len(payload["findings"]["digest"]),
        "receipt": payload["receipt"],
    }, sort_keys=True))
    return 0 if payload["status"].startswith("PASS_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
