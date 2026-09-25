#!/usr/bin/env python3
"""Every affiliate link rail GLP Clinic Scout actually earns on, as DATA.

WHY
---
`build_glp_affiliate_expansion_13.validate_tracking()` knew three networks
(Direct / Katalys / Awin) and raised `network_not_supported` for everything
else, and `build_glp_affiliate_wirein_incremental_v2.PUBLISHER_ID_BY_NETWORK`
carried the same three. Nu Image Medical — approved, 30% revshare, tracking
link in hand — could therefore never be built, and neither could VitalCore
(Refersion), AbundRX (XenTraffic/Everflow), iHealth (GoAffPro) or the CJ estate.
Adding an `elif` per network is how the table got stale in the first place.

So the rails are a TABLE, and each entry states the evidence its shape rests on.
A rail whose link shape has never been observed first-party is present but
`verified=False`: it fails closed by NAME (`rail_link_shape_unverified:<net>`)
instead of being silently absent or, worse, guessed. Naming beats guessing.

EVIDENCE DISCIPLINE
-------------------
Every VERIFIED rail below cites where the shape was observed. Nothing here was
inferred from vendor documentation; each is a link this business has actually
held. Nothing in this module fetches, traverses or clicks a tracking link — it
is pure structural validation on a string.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

#: Our publisher identity per rail, where the rail issues a network-wide one.
#: `None` means the rail issues a PER-PROGRAM id, so the spec must declare it
#: and the builder pins that instead (the Direct lane's existing bargain).
AWIN_PUBLISHER_ID = "2979387"
KATALYS_PUBLISHER_ID = "13031"
CJ_PUBLISHER_ID = "101822575"
TRACKDESK_PUBLISHER_ID = "glp-clinic-scout"
IMPACT_PUBLISHER_ID = "7467025"
AFFILIATI_PUBLISHER_ID = "15309"
XENTRAFFIC_PUBLISHER_ID = "841W6MT"
#: Our publisher token inside the Eden Health International, LLC Everflow
#: white-label tenant. Read first-party out of that tenant's OWN authenticated
#: identity object -- `GET /api/affiliates/affiliate` ->
#: `.relationship.encoded_value` -- on 2026-09-01, alongside
#: `name = "Tilton Group LLC"` and `network_affiliate_id = 62`. It is NOT
#: derived from the tracking link, which is exactly why validating the link
#: against it is a real check rather than a tautology. Every Everflow tenant
#: issues its OWN token, so this constant is Eden-specific and no other tenant
#: may reuse it.
EVERFLOW_EDEN_PUBLISHER_ID = "3RRR23"


class RailError(RuntimeError):
    """A structurally invalid tracking link, named by rail and reason."""


CAPTURE_SCHEMA = "glp.affiliate_tracking_capture.v1"
CAPTURE_REQUIREMENTS_SCHEMA = "glp.affiliate_tracking_capture_requirements.v1"


@dataclass(frozen=True)
class Rail:
    #: Canonical `Network / Route` spelling as the governed Sheet writes it.
    network: str
    #: Extra spellings seen in the wild that mean the same rail.
    aliases: tuple[str, ...] = ()
    #: Where the identity lives: "network_redirector" (a fixed host we do not
    #: own), "merchant_host" (the advertiser's own domain carries our params),
    #: or "tracking_domain" (a rail-owned rotating domain).
    host_class: str = "network_redirector"
    #: Fixed hosts, when the rail has them. Empty ⇒ host comes from the spec.
    hosts: tuple[str, ...] = ()
    #: Our network-wide publisher id, or None when it is per-program.
    publisher_id: str | None = None
    #: Query params any one of which may carry the publisher id.
    publisher_params: tuple[str, ...] = ()
    #: Has this shape been observed on a link WE held? See `evidence`.
    verified: bool = False
    evidence: str = ""
    #: Structural validator. Raises RailError; returns nothing.
    validate: Callable[["Rail", str, str, str | None], None] | None = None
    notes: str = ""
    #: Does the rail expose a publisher-side inventory API we can corroborate a
    #: join against? Trackdesk does not, which is why the wire-backlog worker
    #: has a named non-enumerable branch.
    enumerable: bool = True
    surfaces_publisher_id_in_path: bool = False

    @property
    def key(self) -> str:
        """Short lowercase token used in every error name for this rail."""
        return self.network.split()[0].casefold()


def _https(url: str) -> tuple[str, dict[str, list[str]], str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RailError("tracking_scheme_invalid")
    return (parsed.hostname or ""), parse_qs(parsed.query), parsed.path


def _require_host(rail: Rail, host: str, allowed_host: str | None) -> None:
    if rail.hosts:
        if host not in rail.hosts:
            raise RailError(f"{rail.key}_tracking_invalid_host:{host}")
        return
    if not allowed_host:
        raise RailError(f"{rail.key}_tracking_host_undeclared")
    if host != allowed_host:
        raise RailError(f"{rail.key}_tracking_invalid_host:{host}")


# --------------------------------------------------------------------------
# per-rail structural validators
# --------------------------------------------------------------------------

def _v_katalys(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    host, query, path = _https(url)
    if host != "track.revoffers.com" or path != "/aff_c":
        raise RailError(f"katalys_tracking_invalid:{immutable_id}")
    if query.get("offer_id") != [immutable_id] or len(query.get("aff_id", [])) != 1:
        raise RailError(f"katalys_tracking_invalid:{immutable_id}")


def _v_awin(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    host, query, path = _https(url)
    if host != "www.awin1.com":
        raise RailError(f"awin_tracking_invalid:{immutable_id}")
    if path == "/awclick.php":
        if query.get("mid") != [immutable_id] or len(query.get("id", [])) != 1:
            raise RailError(f"awin_tracking_invalid:{immutable_id}")
    elif path == "/cread.php":
        if query.get("awinmid") != [immutable_id] or len(query.get("awinaffid", [])) != 1:
            raise RailError(f"awin_tracking_invalid:{immutable_id}")
    else:
        raise RailError(f"awin_tracking_invalid:{immutable_id}")


def _v_trackdesk(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """Trackdesk "Direct linking": the merchant URL carries the attribution.

    Observed first-party 2026-08-19 in the authenticated nu-image-medical
    tenant (NUIMAGE-ONBOARD-20260819.md §4a):
      https://nuimagemedical.com/?linkId=lp_716390&sourceId=glp-clinic-scout
                                  &tenantId=nu-image-medical
    `immutable_id` for this rail is the TENANT id — the only stable identity
    Trackdesk exposes publisher-side (there is no publisher API at all).
    """
    host, query, _ = _https(url)
    _require_host(rail, host, allowed_host)
    if query.get("tenantId") != [immutable_id]:
        raise RailError(f"trackdesk_tenant_mismatch:{immutable_id}")
    source = query.get("sourceId") or []
    if len(source) != 1 or not source[0].strip():
        raise RailError(f"trackdesk_source_id_missing:{immutable_id}")
    if not (query.get("linkId") or [""])[0].strip():
        raise RailError(f"trackdesk_link_id_missing:{immutable_id}")


def _v_refersion(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """Refersion: merchant host + ?rfsn=<affiliate_id>.<token>.

    Observed first-party 2026-08-19 (PHASE0-BACKLOG-LEDGER §D1, VitalCore):
      https://vitalcore-health.com/?rfsn=9287190.023e7a
    `immutable_id` is the Refersion affiliate id we hold WITH that merchant.
    """
    host, query, _ = _https(url)
    _require_host(rail, host, allowed_host)
    values = query.get("rfsn") or []
    if len(values) != 1:
        raise RailError(f"refersion_rfsn_missing:{immutable_id}")
    if not re.fullmatch(r"\d+\.[A-Za-z0-9]+", values[0]):
        raise RailError(f"refersion_rfsn_malformed:{immutable_id}")
    if values[0].split(".", 1)[0] != immutable_id:
        raise RailError(f"refersion_affiliate_id_mismatch:{immutable_id}")


def _v_everflow_client(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """Everflow-client (XenTraffic and every white-label tenant on the same rail).

    Observed first-party 2026-08-19 (PHASE0-BACKLOG-LEDGER §5, AbundRX):
      https://www.xfnjwej33dd.com/841W6MT/6S5RGBH/
    Two opaque path segments: <affiliate_token>/<offer_token>. `immutable_id`
    is the OFFER token, because that is what identifies the program; the
    affiliate token is our identity and must be present and non-empty.
    The tracking domain rotates per tenant, so it comes from the spec.
    """
    host, query, path = _https(url)
    _require_host(rail, host, allowed_host)
    if query:
        raise RailError(f"everflow_client_link_carries_query:{immutable_id}")
    segments = [p for p in path.split("/") if p]
    if len(segments) != 2:
        raise RailError(f"everflow_client_path_shape_invalid:{immutable_id}")
    affiliate_token, offer_token = segments
    if not re.fullmatch(r"[A-Za-z0-9]+", affiliate_token):
        raise RailError(f"everflow_client_affiliate_token_invalid:{immutable_id}")
    if affiliate_token != XENTRAFFIC_PUBLISHER_ID:
        raise RailError(f"everflow_client_affiliate_token_drift:{immutable_id}")
    if offer_token != immutable_id:
        raise RailError(f"everflow_client_offer_token_mismatch:{immutable_id}")


def _v_everflow_brand_portal(rail: Rail, immutable_id: str, url: str,
                             allowed_host: str | None) -> None:
    """Everflow white-label BRAND tenant (one advertiser running its own Everflow).

    Observed first-party 2026-09-01 in the Eden Health International, LLC tenant:
      GET https://eden.everflowclient.io/api/affiliates/offers/10
        -> .tracking_url == https://www.ggh1tn9y.com/3RRR23/GTSC3/
      GET https://eden.everflowclient.io/api/affiliates/affiliate
        -> .relationship.encoded_value == 3RRR23   (our publisher token)
    Receipts:
      reports/glp-clinic-scout/affiliate-machine/eden-everflow-approval-20260901/
        04-eden-offer-10-tracking-capture.json
        06-eden-rail-shape-corroboration.json

    Same two-opaque-segment shape as the XenTraffic Everflow-client rail
    (<affiliate_token>/<offer_token>), but the tracking DOMAIN and the affiliate
    token are per-tenant, so both come from the tenant's own authenticated
    capture: the host is declared by the capture, and the affiliate token must
    equal the publisher token this rail pins for that tenant. Nothing here
    requests, resolves, opens or follows the URL.
    """
    host, query, path = _https(url)
    _require_host(rail, host, allowed_host)
    if query:
        raise RailError(f"everflow_brand_portal_link_carries_query:{immutable_id}")
    segments = [p for p in path.split("/") if p]
    if len(segments) != 2:
        raise RailError(f"everflow_brand_portal_path_shape_invalid:{immutable_id}")
    affiliate_token, offer_token = segments
    if not re.fullmatch(r"[A-Za-z0-9]+", affiliate_token):
        raise RailError(f"everflow_brand_portal_affiliate_token_invalid:{immutable_id}")
    expected = rail.publisher_id
    if not expected:
        raise RailError(f"everflow_brand_portal_publisher_token_undeclared:{immutable_id}")
    if affiliate_token != expected:
        raise RailError(f"everflow_brand_portal_affiliate_token_drift:{immutable_id}")
    if not re.fullmatch(r"[A-Za-z0-9]+", offer_token):
        raise RailError(f"everflow_brand_portal_offer_token_invalid:{immutable_id}")
    if offer_token != immutable_id:
        raise RailError(f"everflow_brand_portal_offer_token_mismatch:{immutable_id}")


def _v_cj(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """CJ Affiliate: /click-<publisher_id>-<link_id>[-<extra>] on a CJ domain.

    Observed first-party on our own live site (the Paternity Lab placement
    retired 2026-08-18, glp_site_paused_program_removal.NICHE_FIT_20260818):
      https://www.jdoqocy.com/click-101822575-15214441-1651111940000
    `immutable_id` is the CJ LINK id (the middle field) — the advertiser id is
    not carried in the click URL at all, which is exactly why a validator that
    demanded it would reject every real CJ link.
    """
    host, query, path = _https(url)
    if host not in rail.hosts:
        raise RailError(f"cj_tracking_invalid_host:{host}")
    if query:
        raise RailError(f"cj_link_carries_query:{immutable_id}")
    match = re.fullmatch(r"/click-(\d+)-(\d+)(?:-(\d+))?/?", path)
    if not match:
        raise RailError(f"cj_tracking_invalid:{immutable_id}")
    if match.group(1) != CJ_PUBLISHER_ID:
        raise RailError(f"cj_publisher_id_drift:{match.group(1)}")
    if match.group(2) != immutable_id:
        raise RailError(f"cj_link_id_mismatch:{immutable_id}")


def _v_goaffpro(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """GoAffPro: merchant host + ?ref=<our per-store referral code>.

    Observed first-party 2026-08-19 (PHASE0-BACKLOG-LEDGER §I1, iHealth Labs):
      https://ihealthlabs.com/?ref=ximoguhj
    `immutable_id` is that per-store code — GoAffPro issues a different one per
    merchant site, so there is no network-wide publisher id to pin.
    """
    host, query, _ = _https(url)
    _require_host(rail, host, allowed_host)
    values = query.get("ref") or []
    if len(values) != 1 or not values[0].strip():
        raise RailError(f"goaffpro_ref_missing:{immutable_id}")
    if values[0] != immutable_id:
        raise RailError(f"goaffpro_ref_mismatch:{immutable_id}")


def _v_impact(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """Impact text-link shape observed in our Sesame Care API Ad 1116240.

    Shape: /c/<publisher-id>/<ad-id>/<campaign-id>. ``immutable_id`` is the
    campaign ID. Structural validation never fetches or clicks the link.
    """
    host, query, path = _https(url)
    if host not in rail.hosts or query:
        raise RailError(f"impact_tracking_invalid:{immutable_id}")
    segments = [part for part in path.split("/") if part]
    if (len(segments) != 4 or segments[0] != "c"
            or segments[1] != IMPACT_PUBLISHER_ID
            or not segments[2].isdigit() or segments[3] != immutable_id):
        raise RailError(f"impact_tracking_invalid:{immutable_id}")


def _v_maxbounty(rail: Rail, immutable_id: str, url: str,
                  allowed_host: str | None) -> None:
    """MaxBounty provider-issued href captured from the approved UI builder.

    The path/token is opaque and does not surface campaign 27300.  The verified
    invariant is therefore deliberately narrow: HTTPS, the exact host captured
    alongside this href, and a nonempty attribution path or query.  Validation
    is string-only; the URL is never requested or resolved.
    """
    host, query, path = _https(url)
    _require_host(rail, host, allowed_host)
    if path in {"", "/"} and not query:
        raise RailError(f"maxbounty_tracking_attribution_missing:{immutable_id}")


def _v_affiliati(rail: Rail, immutable_id: str, url: str,
                 allowed_host: str | None) -> None:
    """Affiliati Link creative: /?a=<publisher>&c=<creative>&s1=.

    ``immutable_id`` is the provider-issued creative id captured independently
    from the authenticated campaign configuration. This function only parses a
    string; it never requests or resolves the tracking URL.
    """
    parsed = urlparse(url)
    host, _, path = _https(url)
    _require_host(rail, host, allowed_host)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.fragment or path != "/" or set(query) != {"a", "c", "s1"}:
        raise RailError(f"affiliati_tracking_invalid:{immutable_id}")
    if (query.get("a") != [AFFILIATI_PUBLISHER_ID]
            or query.get("c") != [immutable_id]
            or query.get("s1") != [""]):
        raise RailError(f"affiliati_tracking_invalid:{immutable_id}")


def _v_direct(rail: Rail, immutable_id: str, url: str, allowed_host: str | None,
              tracking_form: str = "query_offer_publisher") -> None:
    host, query, path = _https(url)
    parsed = urlparse(url)
    if not allowed_host:
        raise RailError(f"direct_tracking_host_undeclared:{immutable_id}")
    if host != allowed_host:
        raise RailError(f"direct_tracking_invalid_host:{immutable_id}")
    if tracking_form == "query_offer_publisher":
        if query.get("oid") != [immutable_id] or len(query.get("affid", [])) != 1:
            raise RailError(f"direct_tracking_invalid:{immutable_id}")
        if not query.get("affid", [""])[0].strip():
            raise RailError(f"direct_tracking_publisher_id_empty:{immutable_id}")
    elif tracking_form == "path_referral_code":
        segments = [p for p in path.split("/") if p]
        if parsed.query or parsed.fragment:
            raise RailError(f"direct_tracking_referral_link_carries_query:{immutable_id}")
        if not segments or segments[-1] != immutable_id:
            raise RailError(f"direct_tracking_referral_code_mismatch:{immutable_id}")
    else:
        raise RailError(f"direct_tracking_form_unsupported:{tracking_form}")


#: Our Partnerize publisher namespace, as it appears in the `pubref:` segment of
#: every link the publisher API mints for us. Observed first-party 2026-08-04 on
#: two authenticated (HTTP 200) link readbacks. It is the only part of a
#: Partnerize click URL that binds the link to US, so it is checked.
PARTNERIZE_PUBREF_PREFIX = "glp-scout-"


def _v_partnerize(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    """Partnerize: /click/camref:<camref>/pubref:<ours>/destination:<encoded>.

    Observed first-party 2026-08-04/05 from the Partnerize publisher API, two
    authenticated readbacks, both HTTP 200, in
    `reports/glp-clinic-scout/affiliate-reconciliation-20260804/`
    `partnerize-approved-link-receipt-20260804.json`
    (schema `partnerize_deterministic_link_receipt_v1`, `tracking_clicks: 0`):

      https://emed.prf.hn/click/camref:1011l5NpZi/pubref:glp-scout-1011l6645/
        destination:https%3A%2F%2Fwww.emed.com%2F
      https://cpap.prf.hn/click/camref:1110lKqfz/pubref:glp-scout-1011l6686/
        destination:https%3A%2F%2Fwww.cpap.com

    `immutable_id` is the **camref**. Partnerize has two id namespaces for one
    programme — the console's `campaign_id` (1011l6645) and the click URL's
    `camref` (1011l5NpZi) — and they are not the same string. The camref is what
    identifies the campaign inside a link, so that is what a link validator can
    check; the campaign id appears in no click URL at all. Exactly like CJ and
    the Everflow-client rail, the camref appears in NO application-time URL
    either, so a Partnerize row can only be corroborated by a first-party
    identity capture (see `IDENTITY_SOURCES["Partnerize"]`).

    The brand subdomain rotates per advertiser (`emed.`, `cpap.`), so the host
    is `<label>.prf.hn` or the apex, and the declared host still binds when one
    is supplied.

    WHAT THIS DELIBERATELY REFUSES. The bare `https://prf.hn/click/camref:<x>`
    form that arrives in a Partnerize acceptance EMAIL carries no `pubref` and
    no `destination`. We have never observed that form from an authenticated
    surface, and it binds the click to no publisher identity of ours, so it is
    named rather than waved through: mint the link through the publisher API
    (which the Partnerize lane already does) and the row passes.
    """
    host, query, path = _https(url)
    if query:
        raise RailError(f"partnerize_link_carries_query:{immutable_id}")
    if not (host == "prf.hn" or host.endswith(".prf.hn")):
        raise RailError(f"partnerize_tracking_invalid_host:{host}")
    if allowed_host and host != allowed_host:
        raise RailError(f"partnerize_tracking_invalid_host:{host}")
    segments = [s for s in path.split("/") if s]
    if not segments or segments[0] != "click":
        raise RailError(f"partnerize_tracking_invalid:{immutable_id}")
    fields: dict[str, str] = {}
    for segment in segments[1:]:
        if ":" not in segment:
            raise RailError(f"partnerize_path_segment_unkeyed:{immutable_id}")
        key, value = segment.split(":", 1)
        fields.setdefault(key, value)
    camref = fields.get("camref", "")
    if not camref:
        raise RailError(f"partnerize_camref_missing:{immutable_id}")
    if camref != immutable_id:
        raise RailError(f"partnerize_camref_mismatch:{immutable_id}")
    pubref = fields.get("pubref", "")
    if not pubref:
        raise RailError(f"partnerize_link_missing_pubref_publisher_binding:{immutable_id}")
    if not pubref.startswith(PARTNERIZE_PUBREF_PREFIX):
        raise RailError(f"partnerize_pubref_drift:{immutable_id}")
    if not fields.get("destination"):
        raise RailError(f"partnerize_link_missing_destination:{immutable_id}")


def _v_unverified(rail: Rail, immutable_id: str, url: str, allowed_host: str | None) -> None:
    raise RailError(f"rail_link_shape_unverified:{rail.network}")


#: CJ's click domains. All are CJ-owned redirectors; the estate rotates between
#: them per link, so the set is the allowlist, not one host.
CJ_HOSTS = (
    "www.jdoqocy.com", "www.anrdoezrs.net", "www.dpbolvw.net", "www.kqzyfj.com",
    "www.tkqlhce.com", "www.tqlkg.com", "www.emjcd.com",
    "jdoqocy.com", "anrdoezrs.net", "dpbolvw.net", "kqzyfj.com",
    "tkqlhce.com", "tqlkg.com", "emjcd.com",
)


RAILS: tuple[Rail, ...] = (
    Rail(network="Direct affiliate", aliases=("Direct", "Direct brand", "Direct (Docusign)"),
         host_class="merchant_host", publisher_id=None, publisher_params=("affid",),
         verified=True, evidence="live site placements (transparent-direct lane, 2026-07/08)",
         validate=None,  # handled by validate_tracking's direct branch (2 forms)
         notes="Two forms: query_offer_publisher and path_referral_code."),
    Rail(network="Katalys", host_class="network_redirector", hosts=("track.revoffers.com",),
         publisher_id=KATALYS_PUBLISHER_ID, publisher_params=("aff_id",),
         verified=True, evidence="28 live Katalys placements; katalys_approved_link_resolver",
         validate=_v_katalys),
    Rail(network="Awin", host_class="network_redirector", hosts=("www.awin1.com",),
         publisher_id=AWIN_PUBLISHER_ID, publisher_params=("awinaffid", "id"),
         verified=True, evidence="41 joined Awin programmes; live cread.php/awclick.php links",
         validate=_v_awin),
    Rail(network="Trackdesk", host_class="merchant_host",
         publisher_id=TRACKDESK_PUBLISHER_ID, publisher_params=("sourceId",),
         verified=True,
         evidence="nu-image-medical tenant portal capture 2026-08-19 "
                  "(NUIMAGE-ONBOARD-20260819.md §4a, 65 authenticated captures)",
         validate=_v_trackdesk, enumerable=False,
         notes="No publisher-side discovery API exists; joins cannot be corroborated "
               "by inventory, which is why the wire-backlog worker has a named "
               "non-enumerable branch for this rail."),
    Rail(network="Refersion", host_class="merchant_host",
         publisher_id=None, publisher_params=("rfsn",),
         verified=True,
         evidence="VitalCore Health affiliate 9287190, link vitalcore-health.com/?rfsn="
                  "9287190.023e7a (PHASE0-BACKLOG-LEDGER-20260819.md §D1)",
         validate=_v_refersion,
         notes="Affiliate id is PER MERCHANT, so it is the immutable id, not a "
               "network-wide publisher constant."),
    Rail(network="XenTraffic", aliases=("Everflow client", "Everflow white-label"),
         host_class="tracking_domain", publisher_id=XENTRAFFIC_PUBLISHER_ID,
         verified=True,
         evidence="AbundRX GLP-1 offers, xfnjwej33dd.com/841W6MT/6S5RGBH/ and /6S76C34/ "
                  "(PHASE0-BACKLOG-LEDGER-20260819.md §5)",
         validate=_v_everflow_client, surfaces_publisher_id_in_path=True,
         notes="Tracking domain rotates per tenant, so the spec must declare it."),
    Rail(network="Partnerize", aliases=("Performance Horizon", "PHG"),
         host_class="tracking_domain", publisher_id=None, publisher_params=(),
         verified=True,
         evidence="two authenticated Partnerize publisher-API link readbacks (HTTP 200), "
                  "eMed camref 1011l5NpZi and cpap.com camref 1110lKqfz, "
                  "reports/glp-clinic-scout/affiliate-reconciliation-20260804/"
                  "partnerize-approved-link-receipt-20260804.json, tracking_clicks 0",
         validate=_v_partnerize,
         notes="camref is the immutable id and appears in no application-time URL, so "
               "rows need a first-party identity capture. The brand subdomain of prf.hn "
               "rotates per advertiser."),
    Rail(network="CJ Affiliate", aliases=("CJ", "Commission Junction"),
         host_class="network_redirector", hosts=CJ_HOSTS,
         publisher_id=CJ_PUBLISHER_ID, publisher_params=(),
         verified=True,
         evidence="our own retired Paternity Lab placement, "
                  "jdoqocy.com/click-101822575-15214441-1651111940000",
         validate=_v_cj, surfaces_publisher_id_in_path=True),
    Rail(network="GoAffPro", host_class="merchant_host",
         publisher_id=None, publisher_params=("ref",),
         verified=True,
         evidence="iHealth Labs affiliate 20591328, ihealthlabs.com/?ref=ximoguhj "
                  "(PHASE0-BACKLOG-LEDGER-20260819.md §I1)",
         validate=_v_goaffpro,
         notes="Referral code is per-store."),
    Rail(network="Impact.com", aliases=("Impact", "Impact Radius"),
         host_class="network_redirector", hosts=("sesame-care.pxf.io",),
         publisher_id=IMPACT_PUBLISHER_ID, publisher_params=(),
         verified=True,
         evidence="Impact Partner API GET Ads/1116240 on 2026-08-25; Sesame Care "
                  "campaign 13292 provider-issued /c/<publisher>/<ad>/<campaign> link",
         validate=_v_impact, surfaces_publisher_id_in_path=True,
         notes="The fixed host is intentionally limited to the first-party Sesame "
               "tracking domain actually observed. Add another campaign host only "
               "after its own authenticated API capture."),
    Rail(network="Affiliati", host_class="tracking_domain",
         publisher_id=AFFILIATI_PUBLISHER_ID, publisher_params=("a",),
         verified=True,
         evidence="authenticated Affiliati campaign configuration captures 2026-08-27; "
                  "provider-issued Link creative hrefs inspected as fields only with "
                  "tracking_links_requested=0 and tracking_links_clicked=0",
         validate=_v_affiliati,
         notes="Tracking domain is campaign-specific and must be independently declared; "
               "immutable id is the provider-issued Link creative id."),
    #: ONE Everflow white-label tenant, registered on its own first-party
    #: evidence. Deliberately per-tenant: the generic `Everflow` rail below stays
    #: unverified, so Ro, Wisp, DMS and every other tenant still fail closed by
    #: name until each has its own authenticated capture. "Most likely the same
    #: shape" is not evidence; this entry exists because Eden's actually was.
    Rail(network="Everflow-Eden", aliases=("Everflow Eden", "Everflow-Eden brand portal",
                                           "Direct Everflow tenant \u2014 Eden"),
         host_class="tracking_domain", publisher_id=EVERFLOW_EDEN_PUBLISHER_ID,
         verified=True,
         evidence="Eden Health International, LLC Everflow tenant, authenticated "
                  "2026-09-01: GET /api/affiliates/offers/10 .tracking_url on host "
                  "www.ggh1tn9y.com with path /3RRR23/GTSC3/, and the publisher token "
                  "3RRR23 corroborated INDEPENDENTLY of that link by "
                  "GET /api/affiliates/affiliate .relationship.encoded_value "
                  "(reports/glp-clinic-scout/affiliate-machine/"
                  "eden-everflow-approval-20260901/06-eden-rail-shape-corroboration.json). "
                  "Zero tracking clicks, opens or resolutions.",
         validate=_v_everflow_brand_portal, surfaces_publisher_id_in_path=True,
         notes="Tracking domain is tenant-specific and must be declared by the same "
               "authenticated capture; the offer token is opaque and is the immutable id."),
    Rail(network="Everflow", aliases=("Everflow Marketplace", "Everflow brand portal"),
         host_class="tracking_domain", publisher_id=None,
         verified=False,
         evidence="NONE — four brand-portal applications are in flight "
                  "(Eden r32, Ro r15, Wisp r631, DMS r410); no issued tracking link "
                  "has ever been held, so no shape has been observed first-party.",
         validate=_v_unverified,
         notes="Fails closed by name. When a brand issues a link, capture it, add the "
               "shape here with its citation and flip verified=True — most likely it "
               "is the same two-segment path form as the XenTraffic rail, but 'most "
               "likely' is not evidence."),
    Rail(network="MaxBounty", aliases=("MaxBounty Network",),
         host_class="network_redirector", publisher_id=None,
         verified=True,
         evidence="authenticated MaxBounty campaign 27300 UI builder capture 2026-08-30; "
                  "Native + raw configuration produced a provider href that was inspected "
                  "as a field only and never opened/resolved/clicked",
         validate=_v_maxbounty,
         notes="Provider tokens are opaque and do not carry campaign id. The exact host must "
               "come from the same hash-bound capture; no global host is inferred."),
    Rail(network="HealthTrader", host_class="network_redirector",
         publisher_id=None, verified=False,
         evidence="NONE — 6 offers are visible via /offers/list but the account has no "
                  "per-offer relationship model and no tracking link has been issued "
                  "(PHASE0-BACKLOG-LEDGER-20260819.md §I4).",
         validate=_v_unverified),
)

#: network (casefolded, incl. aliases) -> Rail
_BY_NAME: dict[str, Rail] = {}
for _rail in RAILS:
    for _name in (_rail.network, *_rail.aliases):
        _BY_NAME[_name.strip().casefold()] = _rail


def rail_for(network: str) -> Rail | None:
    name = str(network or "").strip()
    if not name:
        return None
    hit = _BY_NAME.get(name.casefold())
    if hit:
        return hit
    # `Direct affiliate — Brand X` and friends.
    if name.casefold().startswith("direct"):
        return _BY_NAME["direct affiliate"]
    return None


def supported_networks() -> list[str]:
    return sorted(r.network for r in RAILS)


def verified_networks() -> list[str]:
    return sorted(r.network for r in RAILS if r.verified)


def enumerable_networks() -> list[str]:
    return sorted(r.network for r in RAILS if r.enumerable)


def publisher_contract(network: str) -> tuple[str | None, tuple[str, ...]]:
    """The (publisher_id, publisher_params) pair the builders pin against."""
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_publisher_contract_missing:{network}")
    return rail.publisher_id, rail.publisher_params


def requires_declared_host(network: str) -> bool:
    """Must the program spec DECLARE `tracking_host`, or can it be derived?

    Read this together with `spec_allowed_host`, which owns the resolution order
    — this function only answers the authoring question "is this a field the
    author has to supply?", and it must never drift from that resolver:

      * a rail with fixed `hosts` answers for itself   -> False (and a declared
        host would be ignored, so an author must NOT supply one);
      * a `merchant_host` rail DERIVES the host from the spec's own
        `official_url`                                  -> False. Deriving from
        the official URL is what keeps the check honest: the official URL is
        authored from first-party research captures, independently of the
        tracking link, so validating the link against it is a real check. Taking
        it from the tracking link's own hostname would be a tautology and would
        defeat the gate entirely;
      * a `tracking_domain` rail rotates its domain per tenant and the official
        site is NOT that domain, so it can only ever be declared -> True.

    Public so the authoring contract can state that rule without hard-coding a
    list of rails that would drift out of date.
    """
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_not_supported:{network}")
    if rail.hosts:
        return False
    return rail.host_class != "merchant_host"


def capture_requirements(network: str) -> dict[str, Any]:
    """Return the machine-readable evidence contract for one rail capture.

    Capture producers consume this before persisting an href. Host and identity
    requirements therefore live in the rail table instead of being copied into
    every provider adapter.
    """
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_not_supported:{network}")
    if rail.hosts:
        host_binding = "rail_fixed_hosts"
    elif rail.host_class == "merchant_host":
        host_binding = "independent_official_url"
    else:
        host_binding = "same_authenticated_capture"
    required = ["schema", "network", "immutable_id", "tracking_link",
                "tracking_link_sha256", "tracking_clicks", "rail_capture_requirements",
                "rail_capture_requirements_sha256"]
    if host_binding == "same_authenticated_capture":
        required.append("tracking_host")
    elif host_binding == "independent_official_url":
        required.extend(("official_url", "tracking_host"))
    return {
        "schema": CAPTURE_REQUIREMENTS_SCHEMA,
        "network": rail.network,
        "rail_verified": rail.verified,
        "host_class": rail.host_class,
        "host_binding": host_binding,
        "fixed_hosts": list(rail.hosts),
        "immutable_id_binding": "same_capture_separate_field",
        "publisher_id": rail.publisher_id,
        "publisher_params": list(rail.publisher_params),
        "shape_validator": getattr(rail.validate, "__name__", "") if rail.validate else "",
        "required_capture_fields": required,
    }


def _requirements_sha256(requirements: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(requirements), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_tracking_capture(capture: Mapping[str, Any]) -> Rail:
    """Validate a persisted capture against its current rail contract.

    This is string-only validation. A rotating redirector host is accepted only
    when the same capture declares it and the captured href has that exact host.
    """
    network = str(capture.get("network") or "")
    immutable_id = str(capture.get("immutable_id") or "").strip()
    tracking_link = str(capture.get("tracking_link") or "").strip()
    requirements = capture_requirements(network)
    host_binding = str(requirements["host_binding"])
    declared_host = str(capture.get("tracking_host") or "").strip().casefold()
    official_url = str(capture.get("official_url") or "").strip()

    # Validate the rail first so an undeclared rotating host keeps the rail's
    # exact refusal reason (for example maxbounty_tracking_host_undeclared).
    allowed_host = declared_host or None
    if host_binding == "independent_official_url" and official_url:
        expected_host = spec_allowed_host(network, {"official_url": official_url})
        if declared_host and declared_host != expected_host:
            raise RailError(f"rail_capture_host_mismatch:{network}")
        allowed_host = declared_host or expected_host
    rail = validate_link(network, immutable_id, tracking_link, allowed_host)

    for field_name in requirements["required_capture_fields"]:
        value = capture.get(field_name)
        if value is None or value == "" or value == [] or value == {}:
            raise RailError(
                f"rail_capture_required_field_missing:{network}:{field_name}")
    if capture.get("schema") != CAPTURE_SCHEMA:
        raise RailError(f"rail_capture_schema_invalid:{network}")
    recorded_requirements = capture.get("rail_capture_requirements")
    if recorded_requirements != requirements:
        raise RailError(f"rail_capture_requirements_drift:{network}")
    if str(capture.get("rail_capture_requirements_sha256") or "") != (
            _requirements_sha256(requirements)):
        raise RailError(f"rail_capture_requirements_hash_mismatch:{network}")
    link_sha = hashlib.sha256(tracking_link.encode("utf-8")).hexdigest()
    if str(capture.get("tracking_link_sha256") or "") != link_sha:
        raise RailError(f"rail_capture_link_hash_mismatch:{network}")
    try:
        tracking_clicks = int(capture.get("tracking_clicks", -1))
    except (TypeError, ValueError) as exc:
        raise RailError("rail_capture_tracking_clicks_not_zero") from exc
    if tracking_clicks != 0:
        raise RailError("rail_capture_tracking_clicks_not_zero")
    if host_binding == "same_authenticated_capture":
        actual_host = (urlparse(tracking_link).hostname or "").casefold()
        if declared_host != actual_host:
            raise RailError(f"rail_capture_host_mismatch:{network}")
    return rail


def bind_tracking_capture(
    network: str,
    immutable_id: str,
    tracking_link: str,
    *,
    program: str = "",
    official_url: str = "",
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete and validate a capture before its producer writes it.

    The host is parsed from the authenticated href at capture time. Later
    consumers are forbidden to reconstruct or hardcode it.
    """
    requirements = capture_requirements(network)
    clean_link = str(tracking_link or "").strip()
    captured_host = (urlparse(clean_link).hostname or "").casefold()
    host_binding = str(requirements["host_binding"])
    result = dict(base or {})
    result.update({
        "schema": CAPTURE_SCHEMA,
        "network": str(requirements["network"]),
        "program": str(program or result.get("program") or ""),
        "immutable_id": str(immutable_id or "").strip(),
        "tracking_link": clean_link,
        "tracking_link_sha256": hashlib.sha256(clean_link.encode("utf-8")).hexdigest(),
        "rail_capture_requirements": requirements,
        "rail_capture_requirements_sha256": _requirements_sha256(requirements),
    })
    if host_binding == "same_authenticated_capture":
        result["tracking_host"] = captured_host
    elif host_binding == "independent_official_url":
        result["official_url"] = str(official_url or "").strip()
        result["tracking_host"] = spec_allowed_host(
            network, {"official_url": result["official_url"]})
    elif captured_host:
        # Diagnostic only. Fixed-host rails do not rely on this field.
        result["tracking_host"] = captured_host
    validate_tracking_capture(result)
    return result


def registrable_host(url: str) -> str:
    """The https host of a URL, casefolded. Empty for anything that is not https.

    Same rule the wire-in-ready gate has used since 2026-08-20 — kept here so the
    gate and the builder cannot drift apart again.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").casefold()


def spec_allowed_host(network: str, spec: dict) -> str:
    """The host a program spec's tracking link must land on, or "" if the rail
    carries fixed hosts of its own and needs no answer from the spec.

    WHY THIS EXISTS (2026-08-20, Nu Image Medical / Trackdesk). Two lanes were
    resolving this differently:

      * `glp_wire_in_ready_queue` — the gate that decides whether a row may be
        wired at all — derives a `merchant_host` rail's allowed host from the
        governed row's own **Official URL**, and refuses the row when there is
        none (`official_url_missing_for_merchant_hosted_rail`);
      * `build_glp_affiliate_wirein_incremental_v2` — the builder that runs
        afterwards — read `spec["tracking_host"]` and nothing else.

    No program spec has ever carried `tracking_host`, because every rail before
    Trackdesk had fixed hosts and never needed one. So Nu Image passed the gate
    on 2026-08-20T01:56:39Z and then died in the builder with
    `trackdesk_tracking_host_undeclared`, eight attempts running, while three
    authoring cycles a day were commissioned for content nobody was blocked on.

    The resolution order is the gate's, not a new one:
      * an explicitly declared `tracking_host` wins (a `tracking_domain` rail
        rotates per tenant and can ONLY be declared — the official site is not
        the tracking domain, so it may never stand in for it);
      * otherwise a `merchant_host` rail derives it from the spec's own
        `official_url`, which is authored from first-party research captures and
        is established independently of the tracking link. Checking the link
        against it is therefore a real check, not a tautology;
      * a rail that can resolve neither raises `<rail>_tracking_host_undeclared`
        here rather than deep inside the builder, so the block is named.

    Nothing is guessed and no check is loosened: the link still has to sit on
    the host this returns, and `_require_host` still refuses when it cannot.
    """
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_not_supported:{network}")
    if rail.hosts:
        return ""
    declared = str(spec.get("tracking_host") or "").strip().casefold()
    if declared:
        return declared
    if rail.host_class != "merchant_host":
        raise RailError(f"{rail.key}_tracking_host_undeclared")
    derived = registrable_host(spec.get("official_url"))
    if not derived:
        raise RailError(f"{rail.key}_tracking_host_undeclared")
    return derived


def _official_base_domain(official_url: str) -> str:
    """The advertiser's own site, with a leading `www.` stripped. "" if unusable."""
    host = registrable_host(official_url)
    return host[4:] if host.startswith("www.") else host


def program_allowed_hosts(network: str, spec: dict) -> tuple[str, ...]:
    """EVERY host this ONE program's tracking link may land on.

    WHY THIS EXISTS (2026-08-20, Blocker F). `src/provider-review.mjs` carried a
    frozen `DIRECT_TRACKING_HOSTS` literal — a THIRD copy of rail knowledge, keyed
    by network, with no `Trackdesk` key at all. Nu Image Medical's link resolved
    correctly in the worker and in the builder and was then refused by the
    renderer, which is the only place that decides whether the monetized CTA is
    emitted. This function is the one answer all three read.

    It states no new policy. It is exactly what `_require_host` already enforces:

      * a rail with FIXED hosts answers for itself (Awin, Katalys, CJ) — the
        network redirector is the allowlist and the spec is not consulted;
      * every other rail resolves through `spec_allowed_host`, which is the
        wire-in-ready gate's own resolution order (declared `tracking_host`
        first, else the spec's independently authored `official_url`).

    One check is ADDED, and it only ever tightens: on a `merchant_host` rail a
    DECLARED `tracking_host` must sit inside the advertiser's own site — the
    registrable domain of its `official_url`. A declaration can therefore move
    the gate to `my.gobymeds.com` (the referral host GobyMeds issued us, which
    the renderer literal already hard-coded) but can never move it to a domain
    we do not control, which is the whole job the literal was doing.

    Returns a tuple of casefolded hosts. Raises `RailError` rather than
    returning an empty tuple: a program whose hosts cannot be resolved must
    fail by name, never render an unguarded link.
    """
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_not_supported:{network}")
    if rail.hosts:
        return tuple(host.casefold() for host in rail.hosts)
    host = spec_allowed_host(network, spec)
    if not host:
        raise RailError(f"{rail.key}_tracking_host_undeclared")
    declared = str(spec.get("tracking_host") or "").strip().casefold()
    if declared and rail.host_class == "merchant_host":
        base = _official_base_domain(spec.get("official_url"))
        if not base:
            raise RailError(f"{rail.key}_declared_tracking_host_unverifiable:{declared}")
        if declared != base and not declared.endswith(f".{base}"):
            raise RailError(f"{rail.key}_declared_tracking_host_off_official_domain:{declared}")
    return (host,)


def validate_link(network: str, immutable_id: str, url: str,
                  allowed_host: str | None = None,
                  tracking_form: str = "query_offer_publisher") -> Rail:
    """Structurally validate a provider-issued tracking link. NEVER fetches it."""
    rail = rail_for(network)
    if rail is None:
        raise RailError(f"network_not_supported:{network}")
    if rail.network == "Direct affiliate":
        _v_direct(rail, immutable_id, url, allowed_host, tracking_form)
        return rail
    if rail.validate is None:
        raise RailError(f"rail_validator_missing:{rail.network}")
    rail.validate(rail, immutable_id, url, allowed_host)
    return rail


__all__ = [
    "RAILS", "Rail", "RailError", "rail_for", "supported_networks",
    "verified_networks", "enumerable_networks", "publisher_contract",
    "requires_declared_host", "registrable_host", "spec_allowed_host",
    "program_allowed_hosts", "CAPTURE_SCHEMA", "CAPTURE_REQUIREMENTS_SCHEMA",
    "capture_requirements", "bind_tracking_capture", "validate_tracking_capture",
    "validate_link", "AWIN_PUBLISHER_ID", "KATALYS_PUBLISHER_ID",
    "CJ_PUBLISHER_ID", "TRACKDESK_PUBLISHER_ID", "IMPACT_PUBLISHER_ID", "CJ_HOSTS",
    "AFFILIATI_PUBLISHER_ID",
]
