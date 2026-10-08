#!/usr/bin/env python3
"""XenTraffic (Everflow white-label, network 2068) — authenticated-session adapter.

WHY THIS ADAPTER IS DIFFERENT FROM EVERY OTHER ONE IN THIS PACKAGE
------------------------------------------------------------------
Every other network here rides an API key. XenTraffic has none and cannot be
given one: the authenticated `/api/affiliates/users/1319?relationship=all` read
returns `relationship.api.api_key` as an EMPTY STRING, the portal marks the API
tab unavailable, and the tenant exposes no mint control (re-proven
2026-08-19T22:41Z, recorded in `xentraffic-enumeration-20260819.json`). So this
adapter rides the stored authenticated browser session instead
(`~/.hermes/secrets/xentraffic/access.json` -> a Chromium profile directory).

Two facts about that portal, learned the hard way and pinned by tests:

1. **Cookies alone are not enough.** A plain `fetch()` from the logged-in page
   gets **HTTP 401**. The Everflow BFF requires the `x-csrf` header the SPA
   sends on every XHR. This adapter therefore SNIFFS that header off a real
   request the app makes, rather than guessing a cookie name.
2. **The offers TABLE is the cheap read; offer DETAIL is the expensive one.**
   `POST /api/affiliates/offerstable` returns id/name/status/visibility/category
   /payout for 100 offers per page — everything change-detection needs — but no
   tracking link. `GET /api/affiliates/offers/{id}?relationship=all` carries the
   tracking url, the description, the terms and `preview_url`. The daily lane
   therefore lists, diffs, and fetches detail ONLY for what changed or what a
   caller explicitly asks for.

NO PASSWORD IS EVER RESET, CHANGED, OR TYPED HERE. The session is read; if it
has expired the adapter reports `SESSION_EXPIRED` and stops. Nothing in this
module clicks a tracking link (that would manufacture a false click and every
Sheet writer mode refuses a plan whose `tracking_clicks` is not 0).

The pure functions (`classify_offer`, `normalize_offer`, `snapshot_row`,
`diff_snapshots`) take plain dicts and do no I/O, so the tests drive the whole
decision surface without a browser or a network.

ATTRIBUTION GRAMMAR — answered by the network, do not re-ask
-----------------------------------------------------------
Confirmed in writing by Bettina, XenTraffic account manager, 2026-09-15
(`evt_afbf2058d4fd0ab34fdc4aeb6f35`, "Re: XenTraffic Partner ID 4384"):

* **`sub1` carries our click ID on entry, and `{sub1}` returns it in the
  postback.** Whatever value is passed in `sub1` on the click comes back as the
  `{sub1}` macro. Attribution maps correctly as long as `sub1` is the entry slot
  for `cs_click_id`. This is the same shape as Katalys' `sub1..sub5` grammar.
* **Account-level postback status is UNRESOLVED as of 2026-09-15.** Bettina is
  checking whether one already exists before any new postback is added, to avoid
  double-firing. Do not add a postback until she confirms; a duplicate conversion
  is worse than a delayed one.
* **Offer 3277 is held pending advertiser approval.** No tracking gets wired
  until the advertiser approves; she reports that decision back to us.

WHAT STILL GATES EVERY XENTRAFFIC PROGRAM (not attribution)
-----------------------------------------------------------
All 16 XenTraffic programs in `functions/go/program-manifest.js` sit at
`status: candidate_preview_only` with `live_link_allowed: false` and
`candidate_gate: redirector_tos_unverified_for_network:xentraffic`.

The blocker is `redirector_tos_verified: false` — we have no published source URL
establishing that XenTraffic permits our instrumented `/go/{program}` redirect.
Awin and Katalys each cleared this with a documented source
(`scripts/build_glp_full_go_live.py:85`). The ask is outstanding with Bettina.
Attribution being solved does NOT unblock these; the ToS source does.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

ROOT = Path("/home/ubuntu/business")
ACCESS_PATH = Path("/home/ubuntu/.hermes/secrets/xentraffic/access.json")
LOCK_PATH = ROOT / "locks/xentraffic-portal.lock"
PROC_ROOT = Path("/proc")

NETWORK = "XenTraffic"
#: The Sheet's `Network / Route` value for offers reached through this rail.
#: Row 407 (the rail's own row) carries `Network signup`; an OFFER row names the
#: network itself so the wire-in and dedupe lanes can key on it.
NETWORK_ROUTE = "XenTraffic"
PORTAL_URL = "https://xentraffic.everflowclient.io"
OFFERS_PAGE = PORTAL_URL + "/offers"
PAGE_SIZE = 100

#: GLP-1 itself. A hit here makes the offer CORE inventory for this site.
#: `\bglp\b` is word-boundaried on purpose: an unbounded "glp" matches
#: "GLPartners" and every one of our own internal names.
CORE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("glp1", r"\bglp[\s\-–]?1s?\b"),
    ("glp", r"\bglp\b"),
    ("semaglutide", r"semaglutide"),
    ("tirzepatide", r"tirzepatide"),
    ("liraglutide", r"liraglutide"),
    ("retatrutide", r"retatrutide"),
    ("wegovy", r"wegovy"),
    ("ozempic", r"ozempic"),
    ("mounjaro", r"mounjaro"),
    ("zepbound", r"zepbound"),
    ("saxenda", r"saxenda"),
)
#: Medical weight loss WITHOUT a named GLP-1 drug. Real sister-vertical supply
#: for this site (the comparison pages cover medical weight loss broadly), but
#: it is NOT core and the deploy machine must be able to tell the difference.
SISTER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("weight loss", r"weight[\s\-]?loss"),
    ("weight management", r"weight management"),
    ("obesity", r"obesit(y|ies)"),
    ("bariatric", r"bariatric"),
    ("fat loss", r"fat[\s\-]?loss"),
    ("slimming", r"slimming"),
    ("appetite suppressant", r"appetite[\s\-]?suppress\w*"),
)

VERDICT_CORE = "core_glp1"
VERDICT_SISTER = "sister_vertical:medical_weight_loss"
VERDICT_FAIL = "not_fit"

#: The catalogue fields the gate is allowed to read. All four are XenTraffic's
#: OWN published bytes — this is what "first-party catalogue check" means here.
#: The advertiser's marketing site is a different (and weaker) source and is
#: deliberately not consulted by the gate.
CATALOGUE_FIELDS = ("name", "category", "description", "terms")

#: A `category` of "Telehealth" is NOT weight-loss evidence. XenTraffic files
#: ED, TRT, NAD+, hair-loss and fertility offers under it — 74 offers carried
#: `adjacent_weight_loss_or_telehealth` on 2026-08-19 purely because of this
#: label. The gate therefore never passes on category alone unless the category
#: STRING ITSELF matches a pattern (e.g. the literal category "Weight Loss").
CATEGORY_ALONE_IS_NOT_EVIDENCE = ("telehealth", "telemedicine", "health", "nutra")


# ---------------------------------------------------------------------------
# Pure: the fit gate
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """HTML-ish catalogue prose -> comparable plain text."""
    raw = "" if value is None else str(value)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", unescape(raw)).strip()


def _window(haystack: str, start: int, end: int, pad: int = 70) -> str:
    lo = max(0, start - pad)
    hi = min(len(haystack), end + pad)
    return ("…" if lo else "") + haystack[lo:hi].strip() + ("…" if hi < len(haystack) else "")


def _hits(fields: dict[str, str],
          patterns: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for field in CATALOGUE_FIELDS:
        value = fields.get(field) or ""
        if not value:
            continue
        for token, pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if not match:
                continue
            out.append({
                "field": field,
                "token": token,
                "matched": match.group(0),
                "quote": _window(value, match.start(), match.end()),
            })
            break  # one token per field is enough to cite; keep the evidence short
    return out


def classify_offer(offer: dict[str, Any]) -> dict[str, Any]:
    """The GLP / sister fit gate, over XenTraffic's own catalogue fields only.

    Returns {verdict, evidence: [...], reason}. Every PASS carries at least one
    verbatim quote naming the field it came from, so a Sheet cell derived from
    this verdict can be opened and read back.
    """
    fields = {
        "name": _text(offer.get("name")),
        "category": _text(offer.get("category")),
        "description": _text(offer.get("description")),
        "terms": _text(offer.get("terms")),
    }
    core = _hits(fields, CORE_PATTERNS)
    if core:
        return {"verdict": VERDICT_CORE, "evidence": core,
                "reason": "GLP-1 named in XenTraffic's own catalogue fields"}

    sister = _hits(fields, SISTER_PATTERNS)
    # Category-only evidence: allowed when the category STRING matches (the
    # literal "Weight Loss" category is a real statement), refused when the only
    # thing on offer is a generic vertical label like "Telehealth".
    sister = [hit for hit in sister
              if hit["field"] != "category"
              or fields["category"].lower() not in CATEGORY_ALONE_IS_NOT_EVIDENCE]
    if sister:
        return {"verdict": VERDICT_SISTER, "evidence": sister,
                "reason": "medical weight loss named in the catalogue, no GLP-1 drug named"}

    label = fields["category"].lower()
    if label in CATEGORY_ALONE_IS_NOT_EVIDENCE:
        reason = (f"category '{fields['category']}' is a vertical label, not weight-loss "
                  f"evidence — XenTraffic files ED/TRT/NAD+/hair offers under it")
    else:
        reason = "no GLP-1 or weight-loss token in any first-party catalogue field"
    return {"verdict": VERDICT_FAIL, "evidence": [], "reason": reason}


def fit_passes(verdict: str) -> bool:
    return verdict in (VERDICT_CORE, VERDICT_SISTER)


# ---------------------------------------------------------------------------
# Pure: normalisation + diffing
# ---------------------------------------------------------------------------

def normalize_offer(entry: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """One offerstable entry (+ optional detail) -> the shape this lane speaks."""
    detail = detail or {}
    rel = detail.get("relationship") or entry.get("relationship") or {}
    payouts = (((rel or {}).get("payouts") or {}).get("entries")) or []
    offer_id = entry.get("network_offer_id") or detail.get("network_offer_id")
    payout_amount = entry.get("payout_amount")
    payout_type = entry.get("payout_type") or ""
    if payout_amount is None and payouts:
        payout_amount = payouts[0].get("payout_amount")
        payout_type = (payouts[0].get("payout_type") or "").upper()
    return {
        "offer_id": int(offer_id) if offer_id is not None else None,
        "name": str(entry.get("name") or detail.get("name") or ""),
        "status": str(entry.get("offer_status") or detail.get("offer_status") or ""),
        "visibility": str(entry.get("visibility") or detail.get("visibility") or ""),
        "affiliate_status": str((rel or {}).get("offer_affiliate_status") or ""),
        "category": str(entry.get("category")
                        or (((rel or {}).get("category") or {}).get("name")) or ""),
        "payout_type": str(payout_type or "").upper(),
        "payout_amount": payout_amount,
        "payout_display": str(entry.get("payout") or ""),
        "tracking_link": str(detail.get("redirect_tracking_url")
                             or detail.get("tracking_url") or ""),
        "preview_url": str(detail.get("preview_url") or ""),
        "description": _text(detail.get("html_description")),
        "terms": _text(detail.get("terms_and_conditions")),
        "source_url": f"{PORTAL_URL}/offers/{offer_id}",
    }


#: Offer-affiliate statuses that mean "no per-offer approval gate stands between
#: us and running this offer today".
OPEN_AFFILIATE_STATUS = ("public", "approved", "active")


def relationship_of(offer: dict[str, Any]) -> str:
    status = str(offer.get("status") or "").lower()
    affiliate = str(offer.get("affiliate_status") or "").lower()
    if status == "active" and affiliate in OPEN_AFFILIATE_STATUS:
        return "active"
    return affiliate or status or "unknown"


def snapshot_row(offer: dict[str, Any], run_utc: str) -> dict[str, Any]:
    """The row shape `glp/marketplace-enumeration` stores in `marketplace_snapshot`."""
    verdict = offer.get("fit_verdict") or ""
    return {
        "network": "xentraffic",
        "program_id": str(offer.get("offer_id") or ""),
        "program_name": str(offer.get("name") or "")[:160],
        "status": str(offer.get("status") or "")[:60],
        # `relationship` is the word the parent workflow's diff reads. A `public`
        # offer inside our ACTIVE partner account, with a tracking link already
        # issued, is runnable TODAY — that is `active`, one of the parent's
        # TRACKED relationships, so a later pause is seen as adverse rather than
        # filed away as a discovery row nobody chases.
        "relationship": relationship_of(offer)[:40],
        "commission": str(offer.get("payout_display") or "")[:120],
        "last_seen_utc": run_utc,
        "extra": f"{offer.get('category') or ''} | fit={verdict}"[:300],
    }


#: What a daily diff can honestly say about this rail.
DELTA_NEW = "new_offer"
DELTA_GONE = "offer_disappeared"
DELTA_STATUS = "status_change"
DELTA_PAYOUT = "payout_change"


def diff_snapshots(previous: list[dict[str, Any]],
                   current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diff two normalised offer lists. Pure; the caller decides what to do."""
    prev = {str(o.get("offer_id")): o for o in previous if o.get("offer_id") is not None}
    cur = {str(o.get("offer_id")): o for o in current if o.get("offer_id") is not None}
    deltas: list[dict[str, Any]] = []
    for offer_id, offer in sorted(cur.items(), key=lambda kv: int(kv[0])):
        was = prev.get(offer_id)
        if was is None:
            deltas.append({"kind": DELTA_NEW, "offer_id": int(offer_id),
                           "name": offer.get("name"), "to": offer.get("status"),
                           "payout_display": offer.get("payout_display")})
            continue
        if str(was.get("status") or "") != str(offer.get("status") or ""):
            deltas.append({"kind": DELTA_STATUS, "offer_id": int(offer_id),
                           "name": offer.get("name"),
                           "from": was.get("status"), "to": offer.get("status")})
        if _payout_key(was) != _payout_key(offer):
            deltas.append({"kind": DELTA_PAYOUT, "offer_id": int(offer_id),
                           "name": offer.get("name"),
                           "from": was.get("payout_display"),
                           "to": offer.get("payout_display")})
    for offer_id, offer in sorted(prev.items(), key=lambda kv: int(kv[0])):
        if offer_id not in cur:
            deltas.append({"kind": DELTA_GONE, "offer_id": int(offer_id),
                           "name": offer.get("name"), "from": offer.get("status")})
    return deltas


def _payout_key(offer: dict[str, Any]) -> str:
    amount = offer.get("payout_amount")
    try:
        amount = f"{float(amount):.4f}"
    except (TypeError, ValueError):
        amount = str(amount)
    return f"{str(offer.get('payout_type') or '').upper()}:{amount}"


# ---------------------------------------------------------------------------
# I/O: the authenticated session
# ---------------------------------------------------------------------------

class SessionError(RuntimeError):
    """Raised when the stored session cannot answer. Never a silent zero."""


def owned_cdp(profile: Path) -> dict[str, Any] | None:
    """Bind an exact profile owner to its loopback listening socket, not a port guess.

    No lock is removed here. A stale/foreign SingletonLock or an owner without
    a proven listener is a named refusal, never permission to launch again.
    Linux /proc is the same ownership substrate as the existing Awin preflight.
    """
    profile = Path(profile).resolve()
    owners = []
    for proc in PROC_ROOT.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = proc.joinpath("cmdline").read_bytes().decode().split("\0")
            executable = proc.joinpath("exe").readlink()
        except (OSError, UnicodeError):
            continue
        if Path(executable).name not in {"chrome", "chromium", "chromium-browser"}:
            continue
        # Renderer children inherit some flags but do not own the profile.
        if any(arg.startswith("--type=") for arg in args):
            continue
        flags = {}
        for index, arg in enumerate(args):
            if arg.startswith("--"):
                key, sep, value = arg.partition("=")
                flags[key] = value if sep else (args[index + 1] if index + 1 < len(args) else "")
        value = flags.get("--user-data-dir")
        if value and Path(value).resolve() == profile:
            owners.append((proc, flags, str(executable)))
    lock = profile / "SingletonLock"
    if len(owners) > 1:
        raise SessionError("xentraffic_multiple_profile_owners")
    if not owners:
        if os.path.lexists(lock):
            raise SessionError("xentraffic_stale_or_foreign_singleton_lock")
        return None
    proc, flags, executable = owners[0]
    if os.path.lexists(lock):
        try:
            lock_pid = int(os.readlink(lock).rsplit("-", 1)[1])
        except (OSError, ValueError, IndexError) as exc:
            raise SessionError("xentraffic_singleton_lock_unverifiable") from exc
        if lock_pid != int(proc.name):
            raise SessionError("xentraffic_singleton_owner_mismatch")
    try:
        port = int(flags.get("--remote-debugging-port", ""))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise SessionError("xentraffic_owned_profile_without_cdp_port") from exc
    wanted = f"0100007F:{port:04X}"
    try:
        listeners = [line.split() for line in (PROC_ROOT / "net/tcp").read_text().splitlines()[1:]]
        listeners = [row for row in listeners if len(row) >= 10 and row[3] == "0A"
                     and row[1].endswith(f":{port:04X}")]
        sockets = {os.readlink(fd) for fd in (proc / "fd").iterdir()}
    except OSError as exc:
        raise SessionError("xentraffic_cdp_socket_ownership_unverifiable") from exc
    if (len(listeners) != 1 or listeners[0][1] != wanted
            or f"socket:[{listeners[0][9]}]" not in sockets):
        raise SessionError("xentraffic_owned_profile_without_loopback_listener")
    return {"endpoint": f"http://127.0.0.1:{port}", "pid": int(proc.name),
            "profile": str(profile), "executable": executable, "port": port}


def load_access(path: Path = ACCESS_PATH) -> dict[str, Any]:
    access = json.loads(Path(path).read_text())
    for field in ("profile_path", "portal_url", "affiliate_id", "user_id"):
        if not str(access.get(field) or "").strip():
            raise SessionError(f"xentraffic_access_field_missing:{field}")
    return access


class PortalSession:
    """Retained import name; profile/CDP transport is no longer executable."""
    def __init__(self, *args, **kwargs):
        raise SessionError('fresh_ai_browser_vault_stage_required')



# ---------------------------------------------------------------------------
# I/O-shaped, session-injected: the reads
# ---------------------------------------------------------------------------

def list_offers(api: Callable[..., dict[str, Any]],
                page_size: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    """Every offer in the catalogue. Returns (entries, provider_reported_total).

    The reported total is returned NEXT TO the row count on purpose: a partial
    enumeration that reports success looks exactly like a small catalogue, and
    this rail has 381 offers behind a 100-row page cap.
    """
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise SessionError('xentraffic_page_size_invalid')
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = None
    for page in range(1, 52):
        payload = api(
            f"/api/affiliates/offerstable?page={page}&page_size={page_size}"
            f"&order_field=id&order_direction=desc",
            method="POST",
            body={"filters": {"affiliate_status": "__all"}, "search_terms": []},
        )
        published = (payload.get('paging') or {}).get('total_count')
        if type(published) is not int or published < 0:
            raise SessionError('xentraffic_catalog_total_unknown')
        if total is not None and total != published:
            raise SessionError('xentraffic_catalog_total_changed_mid_read')
        total = published
        batch = payload.get('offers')
        if not isinstance(batch, list) or len(batch) > page_size:
            raise SessionError('xentraffic_catalog_page_invalid')
        for row in batch:
            pid = row.get('network_offer_id') if isinstance(row, dict) else None
            if type(pid) is not int or pid <= 0 or pid in seen:
                raise SessionError('xentraffic_catalog_duplicate_or_invalid_identity')
            seen.add(pid)
            entries.append(row)
        if len(entries) == total:
            return entries, total
        if not batch or len(entries) > total:
            raise SessionError('xentraffic_catalog_truncated_or_overflow')
    raise SessionError('xentraffic_catalog_page_budget_exhausted')


def offer_detail(api: Callable[..., dict[str, Any]], offer_id: int) -> dict[str, Any]:
    return api(f"/api/affiliates/offers/{int(offer_id)}?relationship=all")


def completeness(enumerated: int, reported_total: int) -> tuple[str, str]:
    """The same contract `marketplace_supply_sweep` enforces for every network."""
    if type(reported_total) is not int or reported_total < 0:
        return "NO_TOTAL_PUBLISHED", "the portal published no valid total_count"
    if enumerated == reported_total:
        return "COMPLETE", f"{enumerated}/{reported_total} offers enumerated"
    return "INCOMPLETE", f"{enumerated}/{reported_total} offers enumerated — paging lost rows"
