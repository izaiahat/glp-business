#!/usr/bin/env python3
"""Open the col-19 wire-in gate for an approved programme on ANY rail.

THE GAP THIS CLOSES (NUIMAGE-ONBOARD-20260819.md §6 gap #3)
-----------------------------------------------------------
`Wire-In Ready?` (0-indexed column 19, header 20) is the gate the whole build
queue reads. The ONLY thing that could ever set it TRUE was
`katalys_approved_link_resolver.py`, which hard-filters
`Network / Route == "katalys"`. Every non-Katalys approval — Nu Image on
Trackdesk, VitalCore on Refersion, AbundRX on XenTraffic, iHealth on GoAffPro,
the whole CJ estate — was structurally unbuildable. The hand-written
`wire_in_gate` path that did exist was a DIRECT writer, which the single-Sheet-
writer rule forbids outright.

The governed `wire_in_ready` mode (`affiliate_sheet_batch_writer.SHEET_SYNC_MODES`)
is the write half and already exists. This module is the missing PRODUCER: it
reads the governed Sheet, decides which rows have genuinely earned the gate, and
emits `sheet_update_queue` rows for the ONE writer to drain. It never touches the
Sheet itself and it never fetches a tracking link.

WHY THE VALIDATION IS NOT VACUOUS
---------------------------------
Validating a tracking link against an id read OUT OF THAT SAME LINK proves
nothing — it would pass any well-formed string. So the programme's immutable id
must be established by a source INDEPENDENT of the tracking link:

  1. the row's own Apply URL / Publisher Login URL — written at application
     time, months before the portal issued the link, by a different lane; or
  2. a first-party identity capture whose verbatim `quote` is re-read out of the
     cited capture bytes and re-hashed (the same bargain
     `glp_new_approval_research_queue` and `glp_provider_surface_writers` strike).

Only then is the link validated against that id, structurally, via
`glp_affiliate_link_rails`. Two independent cells agreeing on one programme
identity is evidence. One cell agreeing with itself is not.

A rail whose link shape has never been observed first-party (`verified=False`)
refuses BY NAME (`rail_link_shape_unverified:<network>`) rather than passing —
Everflow brand portals and HealthTrader sit there today.

WHAT THIS MODE CAN AND CANNOT SAY
---------------------------------
It says exactly one thing: *the builder can build from this row's own link.*
It writes no status, it can never touch `Wired Live?`, and it refuses any row
that already claims live. Going live still has to happen and prove itself.

    python3 scripts/glp_wire_in_ready_queue.py                 # dry run (default)
    python3 scripts/glp_wire_in_ready_queue.py --apply         # insert queue rows
    python3 scripts/glp_wire_in_ready_queue.py --row 649       # one row

Effects when not `--apply`: none. Effects with `--apply`: datatable inserts only.
Tracking clicks: ALWAYS 0 — nothing here issues a request to any tracking host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
sys.path.insert(0, str(BUSINESS / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import affiliate_payout_policy as payout_policy  # noqa: E402  (standing exclusions)
import affiliate_sheet_batch_writer as writer  # noqa: E402
from affiliate_provider_row_scope import provider_row_scope  # noqa: E402
import affiliate_provider_metadata as provider_metadata
import glp_affiliate_link_rails as rails  # noqa: E402
import glp_maxbounty_lane as lane_identity  # noqa: E402  (canonical slug functions)

SCHEMA = "glp.wire_in_ready_queue.v1"
PLAN_SCHEMA = "affiliate-wire-in-ready-plan-v1"
IDENTITY_CAPTURE_SCHEMA = "glp.wire_in_identity_evidence.v1"
REACTIVATION_CAPTURE_SCHEMA = "glp.katalys_generated_tracking_links.v1"
QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
QUEUED_BY = "glp_wire_in_ready_queue"
RECEIPT_ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/wire-in-ready"
RESEARCH_ROOT = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                 "wire-backlog-worker/program-research")

#: 0-indexed sheet columns this module READS. It writes none of them.
COL_NETWORK = 4
COL_APPLY_URL = 5
COL_OFFICIAL_URL = 6
COL_PROGRAM = 2
COL_SLUG = 9
COL_STATUS = 10
COL_LOGIN_URL = 12
COL_TRACKING_LINK = 14
COL_WIRE_IN_READY = 19
COL_WIRED_LIVE = 20
COL_LINK_QA = 21
COL_NOTES = 23
COL_VERTICAL = 25

#: The Link-QA value a re-activated programme carries. The retained tracking
#: link is UNPROVEN by design until a live probe re-validates it, so the gate
#: must not open on it — that hand-off belongs to
#: `glp_provider_reactivation_restore.py --mode probe`, which is the only thing
#: allowed to clear the flag. Refusing here by that name routes the row to its
#: real owner instead of dead-ending it on a generic reason.
REACTIVATION_LINK_QA = "link_revalidation_required_after_reactivation"

#: Where a rail states a programme's identity in a URL that is NOT the tracking
#: link. `tenant_hosts` = the platform's own domain(s), whose left-most label is
#: the advertiser tenant the PLATFORM chose (not a name we picked).
#: `query_keys` = query parameters where the value IS the programme identity.
#: `path_patterns` = path shapes where a captured group IS the identity.
#:
#: Deliberately conservative: a bare number anywhere in a URL is never an
#: identity, because a price, a date or a UTM value would qualify and a wrong id
#: here keys the entire wire-in ledger to the wrong programme.
IDENTITY_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "Trackdesk": {"tenant_hosts": ("trackdesk.com",), "query_keys": (), "path_patterns": ()},
    "Awin": {"tenant_hosts": (), "query_keys": ("mid", "awinmid", "merchantId"),
             "path_patterns": (r"/merchant-profile/(\d{3,})",
                               r"/awin/merchant/(\d{3,})")},
    # Affiliati's tracking URL carries a Link creative id, not the offer id.
    # The authenticated campaign capture binds those two namespaces, so this
    # rail requires an identity capture rather than inferring from the generic
    # login URL or reading identity back out of the Sheet link.
    "Affiliati": {"tenant_hosts": (), "query_keys": (), "path_patterns": ()},
    "Katalys": {"tenant_hosts": (), "query_keys": ("offer_id", "offerId", "oid"),
                "path_patterns": (r"/offers?/(\d{2,})",)},
    # XenTraffic (Everflow white-label) has TWO ids for one offer and they live
    # in different namespaces: the portal page is `/offers/<numeric offer id>`
    # (3159), while the tracking link carries an opaque OFFER TOKEN (6KMTPWZ) —
    # and `glp_affiliate_link_rails._v_everflow_client` validates against the
    # token, because that is what identifies the programme in a link. Reading
    # the numeric id off the Apply URL therefore produced a *confident wrong
    # answer*: 13 real rows refused with `everflow_client_offer_token_mismatch`
    # on 2026-08-20, which reads like a broken link and is actually two
    # different id systems being compared. So this rail behaves like CJ: the
    # binding between the two ids exists only in the network's own catalogue,
    # and it must arrive as a first-party identity capture (which also declares
    # the rotating `tracking_host` this rail needs).
    "XenTraffic": {"tenant_hosts": (), "query_keys": (), "path_patterns": ()},
    "Refersion": {"tenant_hosts": (), "query_keys": ("affiliate_id", "aid"),
                  "path_patterns": (r"/affiliate/(\d{4,})",)},
    "GoAffPro": {"tenant_hosts": (), "query_keys": ("ref", "referral"),
                 "path_patterns": ()},
    # CJ's click URL carries a LINK id that appears in no application-time URL —
    # the advertiser id on the Apply URL is a different number entirely. So CJ
    # rows can only be corroborated by an identity capture, and they refuse by
    # name until one exists. That is a stated limitation, not a silent pass.
    "CJ Affiliate": {"tenant_hosts": (), "query_keys": (), "path_patterns": ()},
    # Partnerize has two id namespaces for one programme: the console's
    # `campaign_id` (1011l6645) and the click URL's `camref` (1011l5NpZi). The
    # rail validates on the camref, and the camref appears in NO
    # application-time URL — the Apply URL is a per-advertiser PHG console login
    # with no id in it at all. So Partnerize behaves like CJ and XenTraffic: it
    # refuses by name until a first-party identity capture binds the two.
    "Partnerize": {"tenant_hosts": (), "query_keys": (), "path_patterns": ()},
    "Direct affiliate": {"tenant_hosts": (), "query_keys": ("oid", "offer_id"),
                         "path_patterns": ()},
}

#: THE NETWORK'S PORTAL OFFER ID, USED ONLY AS A JOIN KEY.
#:
#: This is deliberately a SEPARATE map from `IDENTITY_SOURCES`, and nothing here
#: ever becomes an `immutable_id`. Comparing a portal id against a link token is
#: what produced 13 confident wrong refusals on the Everflow-client rail in
#: August, and `IDENTITY_SOURCES["XenTraffic"]` still declares no URL sources so
#: that mistake cannot come back.
#:
#: What this DOES do is let a row we renamed for our own bookkeeping find the
#: first-party capture that carries its token. Our Sheet calls row 747
#: "Eden | XenTraffic offer 3051" and row 753 "TMates | XenTraffic offer 3154";
#: the network's catalogue calls them something else entirely, so a name key
#: misses and the row refuses `identity_not_independently_corroborated` — 32 of
#: them did. The Apply URL was written at application time by a different lane
#: and carries `/offers/<id>`, so joining on it is still two independent cells
#: agreeing: the row's own application-time id, and the network's own
#: authenticated catalogue record.
PROVIDER_OFFER_ID_SOURCES: dict[str, tuple[str, ...]] = {
    "XenTraffic": (r"/offers?/(\d{2,})",),
}


#: Katalys is the one live rail whose programme identity appears in NO
#: application-time URL — the marketplace links are all `app.katalys.com`
#: with the offer id only in a JS route. But the network's own authenticated
#: API index IS a first-party source independent of the tracking link, and the
#: keepalive lane already maintains it. So Katalys resolves from that index,
#: by exact offer name, single match only.
KATALYS_INDEX = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                 "katalys-program-index.json")

_TENANT_LABEL = re.compile(r"^([a-z0-9][a-z0-9-]{1,62})\.")
_RESERVED_LABELS = frozenset({"www", "app", "api", "go", "track", "login", "portal",
                              "dashboard", "admin", "my", "partners", "affiliates"})
_ID_SHAPE = re.compile(r"[A-Za-z0-9._-]{2,64}")


class QueueRefusal(RuntimeError):
    """A row that may NOT open the gate, named by reason."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(BUSINESS))
    except ValueError:
        return str(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cell(row: list[Any], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index] if row[index] is not None else "").strip()


def is_true(value: str) -> bool:
    return value.strip().upper() in {"TRUE", "YES", "1"}


def registrable_host(url: str) -> str:
    """The host a merchant-hosted rail's link must land on, from the row's own URL."""
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").casefold()


# ---------------------------------------------------------------------------
# identity — established INDEPENDENTLY of the tracking link
# ---------------------------------------------------------------------------


def identity_from_urls(network: str, urls: dict[str, str]) -> tuple[str, str]:
    """(identity, human source) from application-time URLs. ('', '') if none."""
    spec = IDENTITY_SOURCES.get(network) or {}
    tenant_hosts = spec.get("tenant_hosts") or ()
    query_keys = spec.get("query_keys") or ()
    path_patterns = spec.get("path_patterns") or ()
    for label, url in urls.items():
        url = str(url or "").strip()
        if not url.startswith("https://"):
            continue
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        for platform in tenant_hosts:
            if host.endswith("." + platform):
                match = _TENANT_LABEL.match(host)
                if match and match.group(1) not in _RESERVED_LABELS:
                    return match.group(1), f"{label} tenant subdomain of {platform}"
        query = dict(parse_qsl(parsed.query))
        for key in query_keys:
            value = str(query.get(key) or "").strip()
            if value and _ID_SHAPE.fullmatch(value):
                return value, f"{label} query parameter `{key}`"
        for pattern in path_patterns:
            match = re.search(pattern, parsed.path, re.I)
            if match:
                return match.group(1), f"{label} path `{pattern}`"
    return "", ""


#: Legal-entity and programme-type suffixes a network appends to the same
#: programme. Katalys's index calls row 318 "Happy Head, Inc." and row 324
#: "System Labs Brand Program" where our Sheet calls them "Happy Head" and
#: "System Labs" — the same programme, described two ways. An exact match over
#: raw names refuses both, and that refusal reads like a missing relationship
#: when it is only a naming difference.
#:
#: Normalising is deliberately narrow. It strips trailing corporate form and
#: programme-type words ONLY, never an interior word, so "Happy Head" still does
#: not match "Happy Head Hair" — a different programme with a different offer id
#: is still a different programme, and the single-match rule below still refuses
#: any name that normalises onto two offers.
KATALYS_NAME_SUFFIXES = (
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "co", "co.",
    "corp", "corp.", "corporation", "company", "gmbh", "bv", "b.v.", "sa",
    "s.a.", "plc", "pty", "ag",
    "brand program", "affiliate program", "program", "programme",
)


def normalise_katalys_name(value: str) -> str:
    """Casefolded name with trailing legal/programme suffixes and punctuation removed."""
    text = re.sub(r"[^a-z0-9\s]+", " ", str(value or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in sorted(KATALYS_NAME_SUFFIXES, key=len, reverse=True):
            token = re.sub(r"[^a-z0-9\s]+", " ", suffix).strip()
            if not token:
                continue
            if text == token:
                continue
            if text.endswith(" " + token):
                text = text[: -(len(token) + 1)].strip()
                changed = True
                break
    return text


def katalys_identity(program: str, index_path: Path | None = None) -> tuple[str, str]:
    """(offer_id, source) from the network's own authenticated programme index.

    Exact, case-insensitive name match, and ONLY when exactly one offer carries
    that name. Two offers sharing a name is an ambiguity, and an ambiguous id
    here would key the wire-in ledger to the wrong programme — so it refuses.
    """
    path = index_path or KATALYS_INDEX
    if not Path(path).is_file():
        return "", ""
    try:
        payload = json.loads(Path(path).read_text())
    except ValueError:
        return "", ""
    wanted = str(program or "").strip().casefold()
    if not wanted:
        return "", ""
    programs = payload.get("programs") or []
    hits = [p for p in programs
            if str(p.get("offer_name") or "").strip().casefold() == wanted]
    matched_on = "exact offer name"
    if len(hits) != 1:
        # Fall back to the suffix-normalised comparison. The single-match rule is
        # unchanged: if the normalised name lands on two offers it is still an
        # ambiguity and still refuses, because an ambiguous id here would key the
        # wire-in ledger to the wrong programme.
        normalised = normalise_katalys_name(program)
        if not normalised:
            return "", ""
        hits = [p for p in programs
                if normalise_katalys_name(p.get("offer_name")) == normalised]
        matched_on = "offer name with trailing legal/programme suffix normalised"
    if len(hits) != 1:
        return "", ""
    offer_id = str(hits[0].get("offer_id") or "").strip()
    if not offer_id:
        return "", ""
    captured = str(payload.get("captured_at_utc") or "")
    return offer_id, (f"Katalys authenticated programme index "
                      f"({repo_rel(Path(path))}, captured {captured}), "
                      f"single match on {matched_on}")


#: Directory names whose `*.json` files are capture evidence for THIS producer.
#:
#: DEFECT 2026-09-01 (approvals backfill): recursive discovery knew about
#: `wire-in-ready-captures/` but not `wire-in-identity-captures/`, which is
#: where the Affiliati legacy identity evidence has lived since 2026-08-27. The
#: daily estate scan therefore never even LOADED those three captures, so the
#: quote defect below could not be reported — the rows just said
#: `identity_not_independently_corroborated` with nothing named. A capture
#: directory the loader cannot see is a refusal with no diagnosis.
#:
#: `identity-captures/` is the directory
#: `glp_xentraffic_ingest_queue_20260820.identity_captures` has always written,
#: and it is the ONLY corroboration the Everflow-client rail can ever have —
#: that rail's immutable id is the opaque offer token, which appears in no
#: application-time URL.
CAPTURE_DIR_NAMES = ("wire-in-ready-captures", "wire-in-identity-captures",
                     "identity-captures")


def _capture_candidates(directory: Path, recursive: bool) -> list[Path]:
    if not recursive:
        return sorted(directory.glob("*.json"))
    candidates = set(directory.rglob("*tracking-link*.json"))
    candidates.update(
        path for path in directory.rglob("*.json")
        if path.parent.name in CAPTURE_DIR_NAMES
    )
    return sorted(candidates)


#: One JSON object member, as a human would quote it out of a document:
#: `"creative_id": "169817"`. Anything else is not a member quote and gets no
#: type-tolerant re-read at all.
_QUOTED_MEMBER = re.compile(
    r'^\s*"(?P<key>(?:[^"\\]|\\.)*)"\s*:\s*(?P<value>.+?)\s*,?\s*$', re.S)


def _scalar_identity(value: Any) -> str | None:
    """The comparable form of a JSON scalar, or None when it has none.

    `True`/`False`/`None` deliberately have no comparable form: normalising
    them would let `"x": "true"` satisfy a quote of `"x": true`, which is a
    different fact. Floats keep their float rendering, so `169817.0` never
    satisfies a quote of `169817`.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return value
    return None


def _member_in_document(document: Any, key: str, wanted: str) -> bool:
    """True when `key` appears ANYWHERE in the parsed capture with `wanted`."""
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for node_key, node_value in node.items():
                if node_key == key and _scalar_identity(node_value) == wanted:
                    return True
                stack.append(node_value)
        elif isinstance(node, list):
            stack.extend(node)
    return False


def quote_binds_capture_bytes(quote: str, body: str) -> bool:
    """Does the declared `quote` actually appear in the cited capture bytes?

    The verbatim substring is the strict answer and is tried first. It is also
    the ONLY answer the loader had, and that made it wrong in one specific way:

    DEFECT 2026-09-01 — all three Affiliati `wire-in-identity-captures`
    (40044 Direct Meds / 42896 JRNYS / 43194 MEDVI, captured 2026-08-27)
    declare `"creative_id": "169817"` while the provider's own bytes carry the
    value as a JSON *integer*: `"creative_id": 169817`. The identity is the
    same identity, present in the same first-party document, at the same key —
    only the JSON scalar RENDERING differs. Refusing that is refusing a fact
    the evidence plainly contains, and it silently disqualified every Affiliati
    capture from this producer.

    So a failed substring falls through to ONE narrower re-read: parse the
    quote as a single JSON object member, parse the CITED CAPTURE BYTES as
    JSON, and require that exact key to carry that exact scalar value
    somewhere in the document. Nothing is weakened —

      * the capture's sha256 is still re-hashed before this is ever called;
      * a key that is absent, a value that differs, a quote that is not a
        member, a capture that is not JSON, and every non-scalar or
        boolean/null value all still refuse;
      * `identity in quote` is still checked by the caller afterwards.

    What changes is exactly one thing: `"k": 1` and `"k": "1"` are the same
    fact about the same document.
    """
    if quote in body:
        return True
    match = _QUOTED_MEMBER.match(quote)
    if not match:
        return False
    try:
        key = json.loads('"%s"' % match.group("key"))
        value = json.loads(match.group("value"))
    except ValueError:
        return False
    wanted = _scalar_identity(value)
    if wanted is None:
        return False
    try:
        document = json.loads(body)
    except ValueError:
        return False
    return _member_in_document(document, key, wanted)


def load_identity_captures(
    directory: Path | None, *, recursive: bool = False
) -> dict[str, dict[str, Any]]:
    """Load hash-bound identity captures without deriving identity from a link.

    Generic rail captures carry the immutable id as a separate authenticated
    field and are indexed by the captured link's SHA-256. Legacy identity
    descriptors keep their original quote-in-cited-bytes validation.
    """
    out: dict[str, dict[str, Any]] = {}
    if not directory or not Path(directory).is_dir():
        return out

    def reject(path: Path, reason: str, link_sha: str = "") -> None:
        out.setdefault("_rejected", {"schema": "rejected"})
        out["_rejected"].setdefault("items", []).append(
            {"file": repo_rel(path), "reason": reason})
        if link_sha:
            out.setdefault("_rejected_by_link_sha256", {})[link_sha] = {
                "reason": reason, "_path": repo_rel(path), "_sha256": sha256_file(path)}

    for path in _capture_candidates(Path(directory), recursive):
        if path.name.startswith("_"):
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        # The plan pins the descriptor; the rail validator checks requirements,
        # immutable id, href hash and same-capture host. When it cites separate
        # source bytes, revalidate that binding too before indexing identity.
        if payload.get("schema") == rails.CAPTURE_SCHEMA:
            link = str(payload.get("tracking_link") or "").strip()
            link_sha = hashlib.sha256(link.encode("utf-8")).hexdigest() if link else ""
            try:
                rails.validate_tracking_capture(payload)
                if int(payload.get("tracking_clicks", -1)) != 0:
                    raise rails.RailError("rail_capture_tracking_clicks_not_zero")
                # A descriptor hash does not bind a separately cited campaign.
                # Self-contained rail captures retain their existing contract.
                if any(key in payload for key in ("capture_path", "capture_sha256", "quote")):
                    capture_rel = str(payload.get("capture_path") or "").strip()
                    declared = str(payload.get("capture_sha256") or "").strip()
                    quote = str(payload.get("quote") or "")
                    identity = str(payload.get("immutable_id") or "").strip()
                    if not (capture_rel and declared and quote.strip() and identity):
                        raise rails.RailError("identity_capture_fields_incomplete")
                    capture = BUSINESS / capture_rel
                    try:
                        source_bytes = capture.read_bytes()
                    except OSError:
                        raise rails.RailError(f"identity_capture_missing:{capture_rel}") from None
                    if hashlib.sha256(source_bytes).hexdigest() != declared:
                        raise rails.RailError(f"identity_capture_hash_drift:{capture_rel}")
                    if not quote_binds_capture_bytes(quote, source_bytes.decode("utf-8", "replace")):
                        raise rails.RailError(f"identity_capture_quote_not_in_bytes:{capture_rel}")
                    if identity not in quote:
                        raise rails.RailError(f"identity_capture_id_not_in_quote:{identity}")
            except (ValueError, TypeError, rails.RailError) as exc:
                reject(path, str(exc), link_sha)
                continue
            record = dict(payload)
            record["_path"] = repo_rel(path)
            record["_sha256"] = sha256_file(path)
            record["_captured_at"] = str(
                payload.get("minted_at_utc") or payload.get("captured_at_utc") or "")
            if link_sha:
                current = out.setdefault("_by_link_sha256", {}).get(link_sha)
                if not current or record["_captured_at"] >= current.get("_captured_at", ""):
                    out["_by_link_sha256"][link_sha] = record
            program = str(payload.get("program") or "").strip().casefold()
            if program:
                current = out.get(program)
                if not current or record["_captured_at"] >= current.get("_captured_at", ""):
                    out[program] = record
            continue

        # A legacy MaxBounty tracking file is never accepted as a capture. It is
        # still indexed by href hash so the daily estate scan can report the
        # rail's exact refusal instead of silently falling back to "no identity".
        if (payload.get("status") == "MINTED_NOT_CLICKED"
                and payload.get("program_id") and payload.get("tracking_link")):
            link = str(payload.get("tracking_link") or "").strip()
            link_sha = hashlib.sha256(link.encode("utf-8")).hexdigest()
            try:
                rails.validate_link(
                    "MaxBounty", str(payload.get("program_id") or ""), link,
                    allowed_host=str(payload.get("tracking_host") or "") or None)
                reason = "legacy_tracking_capture_contract_missing"
            except rails.RailError as exc:
                reason = str(exc)
            reject(path, reason, link_sha)
            continue

        if payload.get("schema") != IDENTITY_CAPTURE_SCHEMA:
            continue
        program = str(payload.get("program") or "").strip()
        identity = str(payload.get("immutable_id") or "").strip()
        quote = str(payload.get("quote") or "")
        capture_rel = str(payload.get("capture_path") or "").strip()
        declared = str(payload.get("capture_sha256") or "").strip()
        capture = BUSINESS / capture_rel if capture_rel else None
        reason = ""
        if not (program and identity and quote and capture_rel and declared):
            reason = "identity_capture_fields_incomplete"
        elif capture is None or not capture.is_file():
            reason = f"identity_capture_missing:{capture_rel}"
        elif sha256_file(capture) != declared:
            reason = f"identity_capture_hash_drift:{capture_rel}"
        else:
            body = capture.read_bytes().decode("utf-8", "replace")
            if not quote_binds_capture_bytes(quote, body):
                reason = f"identity_capture_quote_not_in_bytes:{capture_rel}"
            elif identity not in quote:
                reason = f"identity_capture_id_not_in_quote:{identity}"
        if reason:
            reject(path, reason)
            continue
        payload["_path"] = repo_rel(path)
        payload["_sha256"] = sha256_file(path)
        payload["_captured_at"] = str(payload.get("captured_at_utc") or "")
        # Secondary index: the network's own PORTAL offer id. It is never an
        # immutable id (see PROVIDER_OFFER_ID_SOURCES) — it is a join key, so a
        # row we renamed for our own bookkeeping can still find the capture that
        # carries its token.
        provider_offer_id = str(payload.get("provider_offer_id") or "").strip()
        network_key = str(payload.get("network") or "").strip().casefold()
        if provider_offer_id and network_key:
            by_offer = out.setdefault("_by_provider_offer_id", {})
            key = (network_key, provider_offer_id)
            current_offer = by_offer.get(key)
            if not current_offer or str(
                    current_offer.get("_captured_at") or "") <= payload["_captured_at"]:
                by_offer[key] = payload
        # NEWEST FIRST-PARTY CAPTURE WINS, not "whichever file sorted last".
        #
        # The rail-capture branch above already resolves collisions by
        # `_captured_at`; this branch resolved them by directory sort order,
        # which is not a fact about the evidence. Once recursive discovery
        # reaches every `identity-captures/` directory in the tree, an
        # abandoned 2026-08-24 catalogue run would otherwise silently overwrite
        # today's authenticated one purely because `affiliates/` sorts after
        # `affiliate-machine/`.
        key = program.casefold()
        current = out.get(key)
        if current and str(current.get("_captured_at") or "") > payload["_captured_at"]:
            continue
        out[key] = payload
    return out


def load_reactivation_capture(path: Path | None) -> dict[str, dict[str, Any]]:
    """Program name -> verified provider-reminted Katalys link evidence.

    The Katalys generator reads the provider's Create Link modal and never
    requests the tracking URL. A row clears the reactivation marker only when
    that provider-reminted link exactly equals the retained Sheet link.
    """
    if not path or not Path(path).is_file():
        return {}
    capture = Path(path)
    try:
        payload = json.loads(capture.read_text())
    except ValueError:
        return {}
    if payload.get("schema") == "glp.katalys_program_index.v1":
        out: dict[str, dict[str, Any]] = {}
        for record in payload.get("programs") or []:
            statuses = {str(value or "").strip().casefold()
                        for value in record.get("relationship_statuses") or []}
            program = str(record.get("offer_name") or "").strip()
            offer_id = str(record.get("offer_id") or "").strip()
            if program and offer_id and statuses & {"approved", "active", "accepted"}:
                out[program.casefold()] = {
                    "relationship_approved": True,
                    "offer_id": offer_id,
                    "captured_at_utc": str(payload.get("captured_at_utc") or ""),
                    "_path": repo_rel(capture),
                    "_sha256": sha256_file(capture),
                }
        return out
    if (payload.get("schema") != REACTIVATION_CAPTURE_SCHEMA
            or payload.get("status") != "PASS"
            or str(payload.get("traffic_source_id") or "") != "13031"
            or int((payload.get("effects") or {}).get("tracking_clicks", -1)) != 0):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in payload.get("programs") or []:
        program = str(record.get("name") or "").strip()
        link = str(record.get("tracking_link") or "").strip()
        if (program and link and record.get("status") == "GENERATED_VERIFIED"):
            out[program.casefold()] = {
                "tracking_link": link,
                "offer_id": str(record.get("offer_id") or ""),
                "captured_at_utc": str(payload.get("captured_at_utc") or ""),
                "_path": repo_rel(capture),
                "_sha256": sha256_file(capture),
            }
    return out


# ---------------------------------------------------------------------------
# the decision for ONE row
# ---------------------------------------------------------------------------


def assess_row(row_number: int, values: list[Any],
               captures: dict[str, dict[str, Any]] | None = None,
               reactivation: dict[str, dict[str, Any]] | None = None,
               *, ignore_open_gate: bool = False) -> dict[str, Any]:
    """Decide whether row `row_number` may open the col-19 gate.

    Returns a decision dict. `eligible` False always carries a named `reason`;
    there is no silent skip and no unexplained pass.
    """
    captures = captures or {}
    reactivation = reactivation or {}
    row = writer.pad(values)
    program = cell(row, COL_PROGRAM)
    network = cell(row, COL_NETWORK)
    status = cell(row, COL_STATUS)
    link = cell(row, COL_TRACKING_LINK)
    decision: dict[str, Any] = {
        "sheet_row": row_number,
        "program": program,
        "network": network,
        "status": status,
        "vertical": cell(row, COL_VERTICAL),
        "seo_slug": cell(row, COL_SLUG),
        "eligible": False,
        "reason": "",
    }

    if not program:
        decision["reason"] = "blank_row"
        return decision
    excluded = standing_exclusion(row)
    if excluded:
        decision["reason"] = (
            f"standing_exclusion_terms_prohibit_editorial_review_traffic:{excluded}")
        return decision
    scope = provider_row_scope(row_number, row)
    if not scope["eligible"]:
        decision["reason"] = "provider_row_scope:" + scope["reason"]
        decision["provider_scope"] = scope
        return decision
    if is_true(cell(row, COL_WIRED_LIVE)):
        decision["reason"] = "row_already_claims_live"
        return decision
    if is_true(cell(row, COL_WIRE_IN_READY)) and not ignore_open_gate:
        decision["reason"] = "gate_already_open"
        return decision
    if status in writer.SHEET_SYNC_LIVE_STATUS:
        decision["reason"] = f"status_claims_live:{status}"
        return decision
    if status not in writer.SHEET_SYNC_APPROVED_NOT_LIVE_STATUS:
        decision["reason"] = f"status_not_approved_and_unwired:{status or '(blank)'}"
        return decision
    if not link:
        decision["reason"] = "no_tracking_link_on_row"
        return decision
    link_qa = cell(row, COL_LINK_QA)
    if link_qa == REACTIVATION_LINK_QA:
        remint = reactivation.get(program.casefold())
        exact_remint = bool(remint and remint.get("tracking_link") == link)
        approved_readback = bool(remint and remint.get("relationship_approved"))
        if not exact_remint and not approved_readback:
            decision["reason"] = "awaiting_reactivation_link_revalidation"
            decision["detail"] = (
                "the row was re-activated and its retained tracking link is unproven; "
                "supply a PASS Katalys Create Link capture whose provider-reminted link "
                "exactly equals the Sheet link. The tracking URL must never be fetched")
            return decision
        if exact_remint:
            decision["reactivation_revalidation"] = (
                f"provider-reminted Create Link capture {remint['_path']} "
                f"sha256={remint['_sha256']} captured {remint['captured_at_utc']}; "
                "exact link equality; tracking_clicks=0")
        else:
            decision["reactivation_revalidation"] = (
                f"fresh approved-relationship capture {remint['_path']} "
                f"sha256={remint['_sha256']} captured {remint['captured_at_utc']}; "
                f"offer_id={remint['offer_id']}; provider Create Link action unavailable, "
                "so the retained provider-issued link is validated structurally below "
                "without requesting it; tracking_clicks=0")

    rail = rails.rail_for(network)
    if rail is None:
        decision["reason"] = f"network_not_supported:{network or '(blank)'}"
        return decision
    decision["rail"] = rail.network
    if not rail.verified:
        # Naming beats guessing: an unobserved link shape cannot be validated,
        # and a gate opened on an unvalidated link is a build that will emit a
        # broken placement.
        decision["reason"] = f"rail_link_shape_unverified:{rail.network}"
        decision["rail_evidence"] = rail.evidence
        return decision

    identity, source = identity_from_urls(rail.network, {
        "Apply URL": cell(row, COL_APPLY_URL),
        "Publisher Login URL": cell(row, COL_LOGIN_URL),
    })
    link_sha = hashlib.sha256(link.encode("utf-8")).hexdigest()
    capture = (captures.get(program.casefold())
               or (captures.get("_by_link_sha256") or {}).get(link_sha))
    if not capture:
        rejected_capture = (captures.get("_rejected_by_link_sha256") or {}).get(link_sha)
        if rejected_capture:
            decision["reason"] = str(rejected_capture["reason"])
            decision["detail"] = (
                f"the authenticated tracking capture at {rejected_capture['_path']} "
                "failed the rail's capture-time contract")
            return decision
    if not capture:
        # Join by the network's own portal offer id when the row's NAME cannot
        # reach the capture. The capture still supplies the immutable id.
        for pattern in PROVIDER_OFFER_ID_SOURCES.get(rail.network, ()):
            found = None
            for field in (cell(row, COL_APPLY_URL), cell(row, COL_LOGIN_URL),
                          cell(row, COL_PROGRAM), cell(row, COL_NOTES)):
                match = re.search(pattern, str(field or ""), re.I)
                if match:
                    found = match.group(1)
                    break
            if not found:
                continue
            candidate = (captures.get("_by_provider_offer_id") or {}).get(
                (rail.network.casefold(), found))
            if candidate:
                capture = candidate
                decision["identity_join"] = (
                    f"portal offer id {found} read from the row's own "
                    f"application-time cells, joined to the network's authenticated "
                    f"catalogue record; the immutable id still comes from that capture")
                break
    if not identity and rail.network == "Katalys":
        identity, source = katalys_identity(program)
    if not identity and capture:
        identity = str(capture["immutable_id"])
        source = f"first-party identity capture {capture['_path']}"
    if not identity:
        decision["reason"] = "identity_not_independently_corroborated"
        decision["detail"] = (
            "the programme's immutable id could not be established from any cell "
            "other than the tracking link itself; validating a link against an id "
            "read out of that same link proves nothing")
        return decision
    decision["immutable_id"] = identity
    decision["identity_source"] = source

    allowed_host = ""
    if rails.requires_declared_host(rail.network):
        # Rotating/non-fixed redirectors (including MaxBounty) must carry the
        # host in the same hash-bound authenticated capture as the href.
        allowed_host = str((capture or {}).get("tracking_host") or "").strip().casefold()
        if not allowed_host:
            try:
                rails.validate_link(rail.network, identity, link)
            except rails.RailError as exc:
                decision["reason"] = str(exc)
            else:
                decision["reason"] = f"tracking_host_not_declared:{rail.network}"
            decision["detail"] = (
                "this rail has no fixed host; the host must be declared by the same "
                "authenticated capture as the href and may not be inferred later")
            return decision
        decision["allowed_host"] = allowed_host
    elif rail.host_class == "merchant_host" and not rail.hosts:
        allowed_host = registrable_host(cell(row, COL_OFFICIAL_URL))
        if not allowed_host:
            decision["reason"] = "official_url_missing_for_merchant_hosted_rail"
            return decision
        decision["allowed_host"] = allowed_host

    try:
        rails.validate_link(rail.network, identity, link,
                            allowed_host=allowed_host or None)
    except rails.RailError as exc:
        decision["reason"] = str(exc)
        decision["detail"] = ("the row's own tracking link does not have the shape this "
                              "rail issues for that programme id")
        return decision

    decision["eligible"] = True
    # The link this decision is ABOUT. It is what makes the fact a different
    # fact when a rail re-mints, and `queue_row` keys the queue on it.
    decision["tracking_link_sha256"] = link_sha
    decision["rail_evidence"] = rail.evidence
    if decision.get("reactivation_revalidation"):
        decision["rail_evidence"] += "; " + decision["reactivation_revalidation"]
    decision["link_validated_at_utc"] = utc_now()
    decision["tracking_clicks"] = 0
    return decision


# Q41: no hard-coded slug row allowlist. The dedicated metadata producer
# checks each provider x marketplace row and emits only the blank slug cell.


#: SLUG PROVISIONING IS PART OF THE APPROVAL-TO-WIRE-IN CHAIN, not a manual step.
#:
#: THE REPEAT BLOCKER (2026-09-03). `research_binding` below authors nothing
#: A PERMANENTLY EXCLUDED PROVIDER MUST NEVER EARN THE WIRE-IN GATE.
#:
#: WHY THIS EXISTS (2026-09-03, identity-corroboration contract). Row 779,
#: "Fella Health — XenTraffic 3050", sat refused all August for the wrong
#: reason: `identity_not_independently_corroborated`, because nothing had
#: captured the XenTraffic catalogue. The moment that capture landed the row
#: became ELIGIBLE_GOVERNED_QUEUE — and Fella Health is permanently excluded
#: because its terms prohibit review sites, which is the one thing this site
#: is. The exclusion was enforced in `affiliate_payout_policy.traffic_collectible`
#: (winner ranking) and in the census, but not here, so the accidental refusal
#: was the only thing standing between a forbidden provider and column 19.
#:
#: An exclusion held up by an unrelated missing capture is not enforced, it is
#: lucky. This reads the SAME canonical `PERMANENT_EXCLUSIONS` frozenset the
#: payout policy ranks against — no second literal, no second vocabulary — and
#: matches it against the two independent cells that name the provider: the
#: programme cell's leading segment (before the rail's " | " / " — " decoration)
#: and the official URL's own hostname.
_EXCLUSION_DECORATION = re.compile(r"\s*[|\u2014\u2013:]\s*")


def _provider_name_candidates(row: list[Any]) -> tuple[str, ...]:
    """Every casefolded name this row offers for its provider."""
    out: list[str] = []
    program = str(cell(row, COL_PROGRAM) or "")
    if program:
        out.append(program.strip().casefold())
        head = _EXCLUSION_DECORATION.split(program, 1)[0].strip().casefold()
        if head:
            out.append(head)
    host = lane_identity.official_hostname(cell(row, COL_OFFICIAL_URL))
    if host:
        base = host[4:] if host.startswith("www.") else host
        out.append(base)
        label = base.split(".")[0]
        if label:
            out.append(label)
    return tuple(dict.fromkeys(out))


def standing_exclusion(row: list[Any]) -> str:
    """Return the excluded provider name this row is, or "" when it is none."""
    for name in _provider_name_candidates(row):
        if name in payout_policy.PERMANENT_EXCLUSIONS:
            return name
    return ""


#: without a slug, and `glp/research-autowire` authors under the row's EXACT
#: Sheet slug — so a blank column 9 silently stops the research packet, and
#: therefore the deployable page, for a row that is otherwise wire-in ready.
#: It had already been recorded as a named blocker six times: HealSend row 790
#: on Awin, and XenTraffic rows 668, 670, 672, 674 and 695. A blocker that
#: recurs on every new rail is a missing step, not a finding.
#:
#: The slug is NOT a claim about the provider, so it needs no capture: it is a
#: naming convention over facts the row already carries. It is derived exactly
#: the way the MaxBounty chain has always derived it — `provider_review_slug`
#: over the programme name — with one rule ahead of that: if any other row on
#: the Sheet already covers the SAME official domain, reuse that row's slug so
#: one provider never gets two pages. That is the same
#: `same_official_domain_existing_site_slug` basis the chain records.
def provision_seo_slug(values: list[list[Any]], row_number: int,
                       row: list[Any]) -> tuple[str, str]:
    """Naming only; eligibility is enforced by the separate slug-only kind."""
    if cell(row, COL_SLUG):
        return "", ""
    return provider_metadata.derive_slug(values, row_number, row)


def research_binding(values: list[Any]) -> dict[str, Any] | None:
    """Return the exact completed research packet for a governed Sheet row."""
    row = writer.pad(values)
    slug = cell(row, COL_SLUG)
    if not slug:
        return None
    path = RESEARCH_ROOT / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (payload.get("schema") != "program-deep-research-v1"
            or payload.get("status") != "PASS"
            or str(payload.get("program_id") or "") != slug
            or int((payload.get("effects") or {}).get("tracking_clicks", -1)) != 0):
        return None
    return {"path": repo_rel(path), "sha256": sha256_file(path)}


def approved_linked_nonready(values: list[Any]) -> bool:
    row = writer.pad(values)
    return bool(
        cell(row, COL_PROGRAM)
        and cell(row, COL_STATUS) in writer.SHEET_SYNC_APPROVED_NOT_LIVE_STATUS
        and cell(row, COL_TRACKING_LINK)
        and not is_true(cell(row, COL_WIRE_IN_READY))
        and not is_true(cell(row, COL_WIRED_LIVE))
    )


# ---------------------------------------------------------------------------
# plan + queue rows
# ---------------------------------------------------------------------------


def build_plan(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [d for d in decisions if d["eligible"]]
    return {
        "schema": PLAN_SCHEMA,
        "generated_at_utc": utc_now(),
        "generated_by": QUEUED_BY,
        "tracking_clicks": 0,
        "validation": "structural only — no tracking link was fetched",
        "rows": [{
            "sheet_row": d["sheet_row"],
            "program": d["program"],
            "network": d["network"],
            "rail": d["rail"],
            "immutable_id": d["immutable_id"],
            "identity_source": d["identity_source"],
            "rail_evidence": d.get("rail_evidence", ""),
            "allowed_host": d.get("allowed_host", ""),
            "link_validated_at_utc": d["link_validated_at_utc"],
        } for d in eligible],
        "refused": [{
            "sheet_row": d["sheet_row"], "program": d["program"],
            "network": d["network"], "reason": d["reason"],
        } for d in decisions if not d["eligible"] and d["reason"] not in {
            "blank_row", "gate_already_open", "row_already_claims_live"}],
    }


def queue_row(decision: dict[str, Any], plan_path: Path, plan_sha: str) -> dict[str, Any]:
    evidence = {
        "network": decision["network"],
        "immutable_id": decision["immutable_id"],
        "rail_evidence": (f"{decision['rail']} rail, shape observed first-party: "
                          f"{decision.get('rail_evidence', '')}; identity corroborated by "
                          f"{decision['identity_source']}"),
        "link_validated_at_utc": decision["link_validated_at_utc"],
        "wire_in_plan_path": repo_rel(plan_path),
        "wire_in_plan_sha256": plan_sha,
    }
    # A XenTraffic/Everflow-client link lives on the tenant's rotating tracking
    # domain, not on the advertiser's Official URL host. `assess_row` already
    # proves that host against the first-party identity capture and records it in
    # the plan; carry the same bound value into the queue evidence so the writer
    # can independently revalidate the link instead of defaulting to the merchant
    # host and refusing a link this producer just accepted.
    if decision.get("allowed_host"):
        evidence["allowed_host"] = decision["allowed_host"]
    # A blank column 9 stops the research packet, and therefore the page, for a
    # row that is otherwise ready. Carry the derived slug so the SAME governed
    # write that opens the gate also provisions it.
    if decision.get("provisioned_seo_slug"):
        evidence["provisioned_seo_slug"] = decision["provisioned_seo_slug"]
        evidence["provisioned_seo_slug_basis"] = decision.get("provisioned_seo_slug_basis", "")
    host_bound_suffix = "-host-bound" if decision.get("allowed_host") else ""
    # THE QUEUE KEY IS THE FACT, NOT THE DAY.
    #
    # DEFECT 2026-09-02 (approvals backfill). The key used to be
    # `wire-in-ready-r<row>-<YYYYMMDD>[-host-bound]`: one id per row per day.
    # That is fine while a row's link never changes, and wrong the moment it
    # does. `maxbounty_tracking_link` writes a fresh provider-issued link and
    # correctly resets `Wire-In Ready?` to FALSE, because a new link has not
    # been validated yet. The producer then re-validates it, tries to re-open
    # the gate - and `queue_facts` skips the row `already_present`, because the
    # day's id is already sitting in the queue as `applied` for the PREVIOUS
    # link. Six approved GLP-1 rows (709, 717, 723, 726, 729, 730) sat eligible
    # and unqueueable for exactly this reason, with the producer reporting
    # `eligible: 6, queued: 1` and nothing naming why.
    #
    # The link's own sha256 is the right discriminator: it is derived from the
    # state, not from the run, so re-running the producer against an unchanged
    # row still produces the SAME id and still dedupes. A re-mint produces a
    # different id, because it is a different fact about a different link.
    link_key = str(decision.get("tracking_link_sha256") or "")[:12]
    link_suffix = f"-{link_key}" if link_key else ""
    # A slug backfill asserts a DIFFERENT fact than the gate open it follows, so
    # it must not collide with that row's id — row 790's backfill was silently
    # skipped `already_present` against its own gate-open row, which carries the
    # same link sha.
    if decision.get("slug_backfill_only"):
        link_suffix += "-slug"
    return {
        "update_id": f"wire-in-ready-r{decision['sheet_row']}-"
                     f"{decision['link_validated_at_utc'][:10].replace('-', '')}"
                     f"{host_bound_suffix}{link_suffix}",
        "kind": "wire_in_ready",
        "network": decision["network"],
        "program_id": decision.get("seo_slug") or "",
        "program_name": decision["program"],
        "sheet_row": str(decision["sheet_row"]),
        "evidence": json.dumps(evidence, sort_keys=True),
        "status": "queued",
        "queued_by": QUEUED_BY,
        "queued_at_utc": utc_now(),
        "resolved_at_utc": "",
        "note": (f"Wire-in gate earned on the {decision['rail']} rail "
                 f"(immutable id {decision['immutable_id']}); link structurally "
                 f"validated with zero tracking clicks."),
    }


def plan_rows(values: list[list[Any]], *, only_row: int | None = None,
              captures: dict[str, dict[str, Any]] | None = None,
              reactivation: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Assess every business row (header is row 1)."""
    out = []
    for index, row in enumerate(values, 1):
        if index == 1:
            continue
        if only_row is not None and index != only_row:
            continue
        decision = assess_row(index, list(row), captures, reactivation)

        # Slug metadata settles before a later gate-opening write; never race
        # a slug-only source preimage with a gate write in this same batch.
        if decision["eligible"] and not cell(writer.pad(list(row)), COL_SLUG):
            decision["eligible"] = False
            decision["reason"] = "slug_provisioning_precedes_gate"
        out.append(decision)
    return out


def metadata_queue_rows(values, receipt_dir, *, only_row=None, fit_manifests=()):
    """Produce metadata facts; never touch Sheet, approval events or live flags.

    Manifests are content-addressed and never overwritten. A fit ruling takes
    precedence over slug provisioning for its exact row in the same scan.
    """
    if not values or values[0] != writer.HEADERS:
        raise RuntimeError("provider_metadata_header_drift")
    facts, refused, fit_rows = [], [], set()
    root = BUSINESS
    for path in fit_manifests:
        path = Path(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        evidence = {"metadata_manifest_path": repo_rel(path), "metadata_manifest_sha256": sha}
        manifest = provider_metadata.load_manifest(provider_metadata.FIT_KIND, evidence, root)
        index = manifest["sheet_row"]
        if index in fit_rows:
            raise RuntimeError("provider_fit_duplicate_target")
        if only_row is not None and index != only_row:
            raise RuntimeError("provider_fit_outside_selected_row")
        fit_rows.add(index)
        try:
            entry = {"row": index, "program": manifest["source_row"][2], "evidence": evidence,
                     "cells": provider_metadata.manifest_cells(provider_metadata.FIT_KIND, manifest)}
            writer.sheet_sync_plan_rows(provider_metadata.FIT_KIND, {
                "schema": writer.SHEET_SYNC_MODES[provider_metadata.FIT_KIND].plan_schema,
                "authority_sha256": sha, "tracking_clicks": 0, "provider_writes": 0, "rows": [entry]})
            writer.require_sheet_sync_preconditions(provider_metadata.FIT_KIND, values, [entry])
            facts.append(provider_metadata.queue_fact(provider_metadata.FIT_KIND, manifest,
                         repo_rel(path), sha, queued_at=utc_now()))
        except (RuntimeError, ValueError) as exc:
            # A bound target's stale preimage is a row refusal, not permission
            # to apply it or suppress the entire scanner's diagnostic output.
            # fit_rows also prevents a conflicting slug fact for this target.
            refused.append({"sheet_row": index, "kind": provider_metadata.FIT_KIND,
                            "reason": str(exc), "evidence": evidence})
    for index, raw in enumerate(values[1:], 2):
        if index in fit_rows or (only_row is not None and index != only_row):
            continue
        row = provider_metadata.padded(raw)
        if not provider_metadata.slug_eligible(index, row, provider_metadata.SLUG_APPROVED_STATUSES):
            continue
        try:
            slug, basis = provision_seo_slug(values, index, row)
            manifest = {"schema": "glp.provider_metadata.v1", "kind": provider_metadata.SLUG_KIND,
                        "headers": writer.HEADERS, "sheet_row": index, "source_row": row,
                        "seo_slug": slug, "slug_basis": basis}
            body = provider_metadata.canonical(manifest) + "\n"
            sha = hashlib.sha256(body.encode()).hexdigest()
            path = Path(receipt_dir) / "metadata-manifests" / (sha + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_text() != body:
                    raise RuntimeError("provider_metadata_immutable_artifact_drift")
            else:
                path.write_text(body)
            fact = provider_metadata.queue_fact(provider_metadata.SLUG_KIND, manifest,
                                               repo_rel(path), sha, queued_at=utc_now())
            entry = {"row": index, "program": row[2], "evidence": json.loads(fact["evidence"]),
                     "cells": provider_metadata.manifest_cells(provider_metadata.SLUG_KIND, manifest)}
            writer.sheet_sync_plan_rows(provider_metadata.SLUG_KIND, {
                "schema": writer.SHEET_SYNC_MODES[provider_metadata.SLUG_KIND].plan_schema,
                "authority_sha256": sha, "tracking_clicks": 0, "provider_writes": 0, "rows": [entry]})
            writer.require_sheet_sync_preconditions(provider_metadata.SLUG_KIND, values, [entry])
            facts.append(fact)
        except (RuntimeError, ValueError) as exc:
            refused.append({"sheet_row": index, "reason": str(exc)})
    return facts, refused


def insert_queue_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"queued": 0}
    import n8n_dt  # noqa: PLC0415 - only needed on the write path
    queued, skipped, errors = 0, 0, []
    for row in rows:
        try:
            if n8n_dt.rows(QUEUE_TABLE, n8n_dt.eq("update_id", row["update_id"])):
                skipped += 1
                continue
            n8n_dt.insert(QUEUE_TABLE, row)
            queued += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{row['update_id']}:{type(exc).__name__}")
    return {"queued": queued, "already_present": skipped, "errors": errors[:5]}


def run(values: list[list[Any]], *, apply: bool, only_row: int | None,
        captures_dir: Path | None, receipt_dir: Path,
        reactivation_capture: Path | None = None,
        recursive_captures: bool = False,
        fit_manifests: tuple[Path, ...] = ()) -> dict[str, Any]:
    captures = load_identity_captures(captures_dir, recursive=recursive_captures)
    rejected = captures.pop("_rejected", None)
    reactivation = load_reactivation_capture(reactivation_capture)
    decisions = plan_rows(values, only_row=only_row, captures=captures,
                          reactivation=reactivation)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    plan_path = receipt_dir / "wire-in-ready-plan.json"
    metadata_rows, metadata_refused = metadata_queue_rows(
        values, receipt_dir, only_row=only_row, fit_manifests=fit_manifests)
    reclassified = {int(r['sheet_row']) for r in metadata_rows if r['kind'] == provider_metadata.FIT_KIND}
    blocked_fit = {r['sheet_row'] for r in metadata_refused
                   if r.get('kind') == provider_metadata.FIT_KIND}
    # Valid pending fit changes and refused fit targets both precede wiring.
    # Suppress them in the actual plan as well as its emitted queue facts.
    decisions = [
        {**d, 'eligible': False, 'reason': (
            'fit_reclassification_refused' if d['sheet_row'] in blocked_fit
            else 'fit_reclassification_precedes_gate')}
        if d['eligible'] and d['sheet_row'] in reclassified | blocked_fit else d
        for d in decisions
    ]
    # Unresolved/ambiguous metadata input must fail before replacing the prior
    # published plan. A known row refusal still produces a complete report.
    plan = build_plan(decisions)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_sha = sha256_file(plan_path)
    rows = [queue_row(d, plan_path, plan_sha) for d in decisions if d["eligible"]]
    rows = metadata_rows + rows
    selected_rows = {
        index: list(row) for index, row in enumerate(values, 1)
        if index != 1 and (only_row is None or index == only_row)
    }
    stuck_rows = []
    for decision in decisions:
        values_row = selected_rows.get(int(decision["sheet_row"])) or []
        if not approved_linked_nonready(values_row):
            continue
        research = research_binding(values_row)
        stuck_rows.append({
            "sheet_row": decision["sheet_row"],
            "program": decision["program"],
            "network": decision["network"],
            "researched": bool(research),
            "research": research,
            "disposition": ("ELIGIBLE_GOVERNED_QUEUE" if decision["eligible"]
                            else "REFUSED"),
            "reason": ("eligible_for_governed_queue" if decision["eligible"]
                       else decision["reason"]),
        })
    loud_refusals = [row for row in stuck_rows if row["disposition"] == "REFUSED"]
    result = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "plan_path": repo_rel(plan_path),
        "plan_sha256": plan_sha,
        "assessed": len(decisions),
        "eligible": len(rows),
        "supported_rails": rails.supported_networks(),
        "verified_rails": rails.verified_networks(),
        "identity_captures_rejected": (rejected or {}).get("items", []),
        "queue_rows": rows,
        "metadata_refused": metadata_refused,
        "refused": plan["refused"],
        "approved_linked_nonready": stuck_rows,
        "loud_refusals": loud_refusals,
        "status": "BLOCKED_STUCK_ROWS" if loud_refusals or metadata_refused else "PASS",
        "effects": {"sheet_writes": 0, "tracking_clicks": 0, "deploys": 0,
                    "datatable_inserts": 0},
    }
    if apply:
        outcome = insert_queue_rows(rows)
        result["queue"] = outcome
        result["effects"]["datatable_inserts"] = outcome.get("queued", 0)
    else:
        result["queue"] = {"queued": 0, "skipped": "dry_run"}
    (receipt_dir / "run-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def read_live_sheet() -> list[list[Any]]:
    """READ-ONLY, through the drain's own retry-hardened canonical read.

    Reusing `glp_sheet_sync_drain.read_sheet` rather than opening a second read
    path means this producer can never disagree with the writer about what the
    Sheet says, and it inherits the 429 backoff that lane proved it needs.
    """
    from googleapiclient.discovery import build as _build  # noqa: PLC0415
    import glp_sheet_sync_drain as drain  # noqa: PLC0415

    svc = _build("sheets", "v4", credentials=writer.creds(), cache_discovery=False)
    return drain.read_sheet(svc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", nargs="?", const="1", default=None,
                    help="insert the queue rows (default is a dry run)")
    ap.add_argument("--row", type=int, default=None, help="assess ONE sheet row")
    ap.add_argument("--captures", default="",
                    help="directory containing identity or generic rail captures")
    ap.add_argument("--recursive-captures", action="store_true",
                    help="discover hash-bound rail captures below --captures")
    ap.add_argument("--fail-on-stuck", action="store_true",
                    help="return 2 when an approved+linked row is still refused")
    ap.add_argument("--reactivation-capture", default="",
                    help="PASS Katalys Create Link capture for no-click revalidation")
    ap.add_argument("--values", default="",
                    help="JSON file of sheet values (test/offline use)")
    ap.add_argument("--fit-manifest", action="append", default=[],
                    help="explicit hash-bound catalogue fit ruling manifest; metadata only")
    ap.add_argument("--output", default="")
    args = ap.parse_args(argv)

    if args.values:
        values = json.loads(Path(args.values).read_text())
    else:
        values = read_live_sheet()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt_dir = Path(args.output) if args.output else RECEIPT_ROOT / f"run-{stamp}"
    result = run(values, apply=bool(args.apply), only_row=args.row,
                 captures_dir=Path(args.captures) if args.captures else None,
                 receipt_dir=receipt_dir,
                 fit_manifests=tuple(Path(p) for p in args.fit_manifest),
                 reactivation_capture=(Path(args.reactivation_capture)
                                       if args.reactivation_capture else None),
                 recursive_captures=bool(args.recursive_captures))
    print(json.dumps({k: v for k, v in result.items() if k != "queue_rows"},
                     sort_keys=True))
    return 2 if args.fail_on_stuck and result.get("loud_refusals") else 0


if __name__ == "__main__":
    raise SystemExit(main())
