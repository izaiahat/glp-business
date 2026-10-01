#!/usr/bin/env python3
"""ONE answer to "have we already engaged this affiliate program?".

Why this exists (2026-08-18, operator complaint)
------------------------------------------------
The applications lane planned, and actually submitted, a **direct** affiliate
application to `joinfound.com/contact` for a program named **Found** — a program
that is already approved, already wired, and already earning through Katalys at
`/go/found-health` on the live site. The operator then got a #decisions card
asking them to hand-complete an application to a partner we already have.

The dedupe that should have stopped it had three legs, and the one leg that
would have caught it was missing:

  * governed Sheet — MISSED. The Sheet carries Found **twice**: row 110
    ``Found Health`` / Katalys / 13031 / ``live_wired_verified`` (the real one)
    and row 19 ``Found`` / ``Direct/contact verify`` / ``verify_affiliate_route``
    (an un-applied research stub). Dedupe keys on the normalized program NAME,
    and ``norm("Found") == "found" != "foundhealth" == norm("Found Health")``,
    so the live row never covered the stub row.
  * applications ledger — MISSED. The Katalys relationship predates the ledger,
    so no `katalys:13031` row exists to collide with.
  * sheet_update_queue — MISSED, same reason.
  * **live site program registry — NEVER CONSULTED AT ALL.**

The site registry is the one store that ties all three spellings together:
release `data/program-registry.json` holds program id ``found-health`` with
display name ``Found``, `functions/go/program-manifest.js` routes it, the
deployed `https://glpclinicscout.com/go/_manifest` lists it, and
`affiliate-activation-selection.json` resolves it to network Katalys. Any ONE of
`norm("found")`, `norm("found-health") == "foundhealth"`, or the network pair
would have skipped the candidate.

So: this module is the single source of truth, and every lane that can cause a
provider-side application consumes it — `glp_application_machine.gate_and_dedupe`
(planning) and `marketplace_adapters.portal_apply.drain` (execution, the last
gate before the mutation). A lane may not carry its own private dedupe.

Design rules
------------
* **The site leg is mandatory.** It is a pure local file read of the current
  release; if it cannot be built, `build()` raises. Applying to a program we
  already earn from is exactly the defect this module exists to prevent, so an
  unreadable site registry is a stop, not a shrug.
* **Every other leg is best-effort but recorded.** `DedupeIndex.legs` says which
  legs ran, how many identities each contributed, and why any leg is missing.
  A caller can require more (`require=(...)`).
* **Nothing here decides fit, policy, or truth.** It answers one question and
  cites the leg and the exact identity that answered it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "scripts"
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"
QUEUED_INDEX = MACHINE_DIR / "applications/queued-index.json"

#: Written by whichever lane can reach Google, read by the lanes that cannot
#: (the Playwright venv has no googleapiclient). A mirror is evidence of the
#: last successful read, never a substitute for one: it carries its own stamp
#: and `DedupeIndex.legs["sheet"]` says when it was taken.
SHEET_MIRROR = MACHINE_DIR / "dedupe-sheet-mirror.json"
SUPPRESSION_LEDGER = MACHINE_DIR / "relationship-suppression-ledger.json"

#: Sheet statuses meaning "not applied to yet". Mirrors
#: glp_application_machine.UNAPPLIED_SHEET_STATUSES; kept here so the offline
#: mirror path does not need to import the Sheets client.
UNAPPLIED_SHEET_STATUSES = {
    "", "ready_to_apply", "verify_affiliate_route",
    "provider_not_joined_reapply_decision_required",
}

LEG_SITE = "site"
LEG_SHEET = "sheet"
LEG_SUPPRESSED = "suppressed"
LEG_LEDGER = "ledger"
LEG_QUEUE = "queue"
#: Ordered by authority: the citation the operator reads comes from the first
#: leg that can answer, and "we already earn from them" outranks "we filed a row".
ALL_LEGS = (LEG_SITE, LEG_SHEET, LEG_SUPPRESSED, LEG_LEDGER, LEG_QUEUE)
#: The legs the EXECUTOR drain consults. `ledger` is excluded because the row it
#: is asking about is itself in the ledger; the drain has its own replay guard.
EXECUTOR_LEGS = (LEG_SITE, LEG_SHEET, LEG_SUPPRESSED, LEG_QUEUE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_by_path(name: str, path: Path):
    """Import a sibling module WITHOUT mutating `sys.path`.

    The executor drain calls into this module inside its credential-carrying
    critical section, where `scripts` is deliberately absent from `sys.path`
    (pinned by tests/test_item_e_shared_ledger_writer_lock.py). Widening the
    import surface of that process to get a dedupe index is not a trade worth
    making, so the sibling is loaded from its own file.
    """
    import importlib.util
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


def norm(value: Any) -> str:
    """The identity spelling every leg agrees on: alphanumerics, lowercased."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


# ---------------------------------------------------------------------------
# Cross-network BRAND identity
# ---------------------------------------------------------------------------
#
# 2026-08-18, second incident: Awin emailed an approval for **HealthRX**
# programme 129105 — a brand we have been earning from through **Katalys** for
# weeks, live on the site as `/go/healthrx`. Every dedupe axis we had was
# network-scoped (`awin:129105` never collides with `katalys:<id>`), so a brand
# we already monetize looked "not joined" and the machine applied again.
#
# The fix is a NETWORK-FREE identity: the brand's normalized name and its
# primary domain. A brand match on ANY network blocks the application.

#: Corporate/marketing suffixes that are not part of the brand. Stripped only to
#: build the FUZZY key — never to build the exact key — because "Form Health"
#: and "Form" may or may not be the same company, and guessing is not allowed.
_BRAND_SUFFIXES = (
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "co", "corp",
    "corporation", "company", "plc", "gmbh", "bv", "sa", "srl", "pty",
    "us", "usa", "uk", "row", "global", "official", "store", "shop", "online",
    "affiliate", "affiliates", "program", "programme", "partners", "partner",
    "health", "healthcare", "wellness", "care", "clinic", "clinics", "medical",
    "med", "meds", "rx", "pharmacy", "telehealth", "labs", "lab", "nutrition",
    "nutritionals", "supplements",
)
#: Hosts that identify an affiliate NETWORK, never the merchant. A domain leg
#: keyed on one of these would merge every program on that network into one
#: brand, which is the opposite of identity.
_NON_BRAND_HOSTS = frozenset({
    "track.revoffers.com", "revoffers.com", "awin1.com", "ui.awin.com", "awin.com",
    "anrdoezrs.net", "dpbolvw.net", "jdoqocy.com", "kqzyfj.com", "tkqlhce.com",
    "linksynergy.com", "click.linksynergy.com", "shareasale.com", "sjv.io",
    "pxf.io", "impact.com", "partnerstack.com", "refersion.com", "goaffpro.com",
    "uppromote.com", "clickbank.net", "digistore24.com", "sovrn.com",
    "app.katalys.com", "katalys.com", "levanta.io", "flexoffers.com",
    "docs.google.com", "forms.gle", "airtable.com", "typeform.com",
})
#: White-label platform hosts where the SUBDOMAIN is the merchant.
#:
#: Found 2026-08-18 by running the Everflow brand-portal lane: every one of the
#: twelve doors is `<brand>.everflowclient.io`, so the ordinary registrable-domain
#: rule collapsed all twelve to `everflowclient.io` and they all collided with
#: each other AND with Sheet row 410 (Digital Media Solutions, whose signup URL
#: is `digitalmediasolutions.everflowclient.io`). Reverse Health and Wisp - which
#: have no Sheet row at all - were being skipped as "already engaged".
#:
#: They are NOT `_NON_BRAND_HOSTS`: dropping the domain identity entirely would
#: throw away a real, distinguishing fact. The right key is the FULL host, which
#: keeps `eden.everflowclient.io` and `ro.everflowclient.io` distinct. A bare
#: apex with no tenant label is the platform itself and carries no brand.
_TENANT_HOSTS = frozenset({
    "everflowclient.io",
    "trackdesk.com",
    "aspireiq.com",
})
#: Public-suffix pieces we must not treat as the registrable label.
_TLD_TAIL = frozenset({"com", "net", "org", "co", "io", "us", "health", "care",
                       "shop", "store", "app", "ai", "md", "life", "uk", "ca"})


def brand_key(name: Any) -> str:
    """A network-free brand key: normalized, with corporate suffixes stripped.

    `HealthRX` -> `healthrx`; `Health RX (US)` -> `healthrx`;
    `Zentein Nutrition Inc.` -> `zentein`; `Found Health` -> `found`.

    Deliberately lossy, and therefore only ever used for the AMBIGUOUS tier —
    never to block outright on its own.
    """
    tokens = [token for token in re.split(r"[^a-z0-9]+", str(name or "").lower()) if token]
    # Split a run-together brand like "healthrx" only when it is already one
    # token; otherwise strip whole suffix tokens from the tail and the head.
    while len(tokens) > 1 and tokens[-1] in _BRAND_SUFFIXES:
        tokens.pop()
    while len(tokens) > 1 and tokens[0] in _BRAND_SUFFIXES:
        tokens.pop(0)
    key = "".join(tokens)
    # Floor. `Health RX (US)` strips down to `health`, which is a category and
    # would fuzzy-match half the vertical. A key that is itself a generic word,
    # or too short to identify anybody, is no key at all.
    if len(key) < 5 or key in _BRAND_SUFFIXES:
        return ""
    return key


def domain_key(url: Any) -> str:
    """The registrable domain of a merchant URL, or "" for a network host.

    `https://www.healthrx.com/affiliates` -> `healthrx.com`
    `https://track.revoffers.com/aff_c?...` -> `` (a network, not a brand)
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "https://" + text
    host = text.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0].split("@")[-1]
    host = host.split(":", 1)[0].strip().lower().rstrip(".")
    if not host or "." not in host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if host in _NON_BRAND_HOSTS or any(host.endswith("." + bad) for bad in _NON_BRAND_HOSTS):
        return ""
    for platform in _TENANT_HOSTS:
        if host == platform:
            return ""               # the platform apex names no merchant
        if host.endswith("." + platform):
            return host             # the tenant label IS the merchant identity
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in _TLD_TAIL and parts[-1] in _TLD_TAIL:
        parts = parts[-3:]          # co.uk style
    elif len(parts) > 2:
        parts = parts[-2:]
    return ".".join(parts)


class DedupeSkip(dict):
    """A verdict. Truthy means STOP; `None` from `verdict()` means proceed."""

    @property
    def reason(self) -> str:
        return str(self.get("reason") or "")

    @property
    def leg(self) -> str:
        return str(self.get("leg") or "")


def _skip(leg: str, reason: str, matched: str, evidence: str = "") -> DedupeSkip:
    return DedupeSkip(leg=leg, reason=reason, matched_identity=matched, evidence=evidence)


# ---------------------------------------------------------------------------
# Leg 1 (mandatory): what the LIVE SITE already serves
# ---------------------------------------------------------------------------


def site_identities(*, check_live_manifest: bool = False) -> dict[str, Any]:
    """Every identity spelling for every program the site actually serves.

    Sources, all from the current release the site was built from:
      * `data/program-registry.json`  — display name + program id, `live` only
      * `functions/go/program-manifest.js` — the routes the worker actually has
      * `data/affiliate-activation-selection.json` and friends, via
        `identity_map()` — the network + network program id behind each route

    `check_live_manifest=True` additionally fetches the DEPLOYED
    `/go/_manifest`, which is the only thing a reader can truly reach. The fetch
    is advisory: a network failure records itself and does not weaken the leg,
    because the release read already proves the program is ours.
    """
    liveness = _load_by_path("glp_program_liveness_check",
                             SCRIPTS / "glp_program_liveness_check.py")

    release = liveness.release_root()
    go = liveness.go_manifest_programs(release)
    registry = liveness.registry_live_programs(release)
    mapping = liveness.identity_map(release)

    names: dict[str, str] = {}
    pairs: dict[tuple[str, str], str] = {}
    domains: dict[str, str] = {}
    brands: dict[str, str] = {}

    #: program_id -> merchant host, from the release's own held-link manifest.
    hosts: dict[str, str] = {}
    try:
        held = json.loads((release / "data/affiliate-held-link-manifest.json").read_text())
        for row in held.get("programs") or []:
            host = domain_key(row.get("destination_host"))
            if host:
                hosts[str(row.get("content_program_id") or "")] = host
    except (OSError, ValueError):
        pass

    program_ids = sorted(set(go.get("programs") or {}) | set(registry))
    for program_id in program_ids:
        row = registry.get(program_id, {})
        display = str(row.get("display_name") or "").strip()
        cite = f"site program `{program_id}`" + (f" (\"{display}\")" if display else "")
        identity = mapping.get(program_id) or {}
        network_label = str(identity.get("network") or "").strip()
        relationship = (f"live on the site" + (f", monetized via {network_label}" if network_label else ""))
        cite_full = f"{cite} — already {relationship}"
        for spelling in (program_id, display):
            key = norm(spelling)
            if key:
                names.setdefault(key, cite_full)
            fuzzy = brand_key(spelling)
            if fuzzy and fuzzy != key:
                brands.setdefault(fuzzy, cite_full)
        network = norm(network_label)
        network_program_id = str(identity.get("network_program_id") or "").strip().casefold()
        if network and network_program_id:
            pairs.setdefault((network, network_program_id),
                             f"{cite} = {network_label}:{network_program_id} — already {relationship}")
        host = hosts.get(program_id) or domain_key(identity.get("tracking_url"))
        if host:
            domains.setdefault(host, f"{cite} — already {relationship} (merchant domain {host})")

    leg: dict[str, Any] = {
        "ok": True,
        "release_root": str(release),
        "go_manifest_sha256": go.get("manifest_sha256"),
        "program_count": len(program_ids),
        "name_keys": len(names),
        "network_pairs": len(pairs),
        "live_manifest": {"checked": False},
    }
    if check_live_manifest:
        try:
            response = liveness.http_get(liveness.LIVE_MANIFEST_URL)
            payload = json.loads(response.get("body") or "{}")
            deployed = payload.get("program_ids") or list((payload.get("programs") or {}))
            for program_id in deployed:
                key = norm(program_id)
                if key:
                    names.setdefault(key, f"deployed /go/_manifest program `{program_id}`")
            leg["live_manifest"] = {
                "checked": True, "ok": True, "url": liveness.LIVE_MANIFEST_URL,
                "manifest_sha256": payload.get("manifest_sha256"),
                "program_count": len(deployed),
            }
        except Exception as exc:  # noqa: BLE001 — advisory only
            leg["live_manifest"] = {"checked": True, "ok": False,
                                    "error": f"{type(exc).__name__}:{exc}"[:200]}
    leg["domains"] = len(domains)
    leg["brand_keys"] = len(brands)
    return {"names": names, "pairs": pairs, "domains": domains, "brands": brands, "leg": leg}


# ---------------------------------------------------------------------------
# Leg 2: the governed Sheet
# ---------------------------------------------------------------------------


def write_sheet_mirror(engaged_names: Iterable[str],
                       engaged_ids: Iterable[tuple[str, str]],
                       *, row_count: int = 0, path: Path | None = None,
                       engaged_rows: Iterable[dict[str, Any]] = ()) -> Path:
    """Persist a successful Sheet read so offline lanes still get this leg.

    `engaged_rows` carries the per-row identity the cross-network legs need:
    the program's own name, network label, status and URLs. Without it the
    mirror can only answer the network-scoped question, which is exactly the
    question that missed HealthRX.
    """
    path = Path(path or SHEET_MIRROR)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "glp.application_dedupe.sheet_mirror.v2",
        "captured_at_utc": utc_now(),
        "row_count": int(row_count),
        "engaged_names": sorted(set(engaged_names)),
        "engaged_ids": sorted({f"{network}\t{program_id}"
                               for network, program_id in engaged_ids}),
        "engaged_rows": sorted(
            ({"name": str(row.get("name") or ""),
              "network": str(row.get("network") or ""),
              "status": str(row.get("status") or ""),
              "program_id": str(row.get("program_id") or ""),
              "sheet_row": row.get("sheet_row"),
              "official_url": str(row.get("official_url") or ""),
              "apply_url": str(row.get("apply_url") or "")}
             for row in engaged_rows),
            key=lambda row: (row["name"], str(row["sheet_row"]))),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def read_sheet_mirror(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or SHEET_MIRROR)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


#: Sheet statuses that record OUR outreach and NO answer from the provider.
#:
#: These are not relationships. `partnership_form_submitted_awaiting_response`
#: means we typed into somebody's website contact form and nobody replied - it
#: does not mean we hold an affiliate account, and it does not mean the provider
#: has seen an application at all. When a DIFFERENT, independently verified
#: application door exists for the same brand (Eden's own Everflow publisher
#: portal, versus the contact form we used), walking through it is a follow-up,
#: not a duplicate.
#:
#: This is deliberately tiny and deliberately opt-in: `verdict()` still blocks on
#: these by default, and a caller must pass `allow_follow_up=True` AND record the
#: citation. It is NOT a way to re-apply over `applied`, `pending`, `approved`,
#: `live` or `rejected` - those are real relationships and stay blocked on every
#: path.
FOLLOW_UP_ELIGIBLE_STATUSES = frozenset({
    "partnership_form_submitted_awaiting_response",
})


def _engaged_row_identities(rows: Iterable[dict[str, Any]], source: str,
                            names: dict[str, str], pairs: dict[tuple[str, str], str],
                            domains: dict[str, str], brands: dict[str, str],
                            collisions: dict[str, list[dict[str, Any]]],
                            follow_up_only: dict[str, str] | None = None) -> None:
    """Fold each engaged Sheet row into every identity axis it can answer.

    Relationship state is carried through VERBATIM and is multi-valued on
    purpose: `applied`, `pending`, `approved`, `live_wired_verified`, `rejected`
    and `suppressed` are six different existing relationships and the operator's
    card should say which one it is, not "already_relationship".
    """
    by_brand: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        network = str(row.get("network") or "").strip()
        status = str(row.get("status") or "").strip() or "engaged"
        where = f"row {row.get('sheet_row')}" if row.get("sheet_row") else "an engaged row"
        cite = (f"{source}: {where} `{name}`"
                + (f" on {network}" if network else "")
                + f" is already `{status}`")
        key = norm(name)
        if key:
            names.setdefault(key, cite)
        program_id = str(row.get("program_id") or "").strip()
        if network and program_id:
            pairs.setdefault((norm(network), program_id.casefold()), cite)
        for url in (row.get("official_url"), row.get("apply_url")):
            host = domain_key(url)
            if host:
                domains.setdefault(host, f"{cite} (merchant domain {host})")
        fuzzy = brand_key(name)
        if fuzzy:
            brands.setdefault(fuzzy, cite)
            by_brand.setdefault(fuzzy, []).append(
                {"name": name, "network": network, "status": status,
                 "sheet_row": row.get("sheet_row"), "program_id": program_id})
    # An identity is follow-up eligible only when EVERY engaged row answering it
    # is an unanswered outreach. One real relationship anywhere on the key and
    # the whole key stays blocked - a brand we already earn from can never be
    # talked into "we only sent them a form".
    if follow_up_only is not None:
        by_key: dict[str, list[str]] = {}
        for row in rows:
            key = norm(row.get("name"))
            if key:
                by_key.setdefault(key, []).append(
                    str(row.get("status") or "").strip() or "engaged")
        for key, statuses in by_key.items():
            if statuses and all(s in FOLLOW_UP_ELIGIBLE_STATUSES for s in statuses):
                follow_up_only.setdefault(key, ",".join(sorted(set(statuses))))
    # A brand with two engaged rows is a COLLISION to report, never a merge.
    # (Calibrate is the cautionary case: two rows, different network labels.)
    for fuzzy, hits in by_brand.items():
        distinct = {(hit["name"], hit["network"], str(hit["sheet_row"])) for hit in hits}
        if len(distinct) > 1:
            collisions.setdefault(fuzzy, []).extend(hits)


def _sheet_leg(sheet_index: dict[str, Any] | None, mirror_path: Path | None,
               collisions: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    names: dict[str, str] = {}
    pairs: dict[tuple[str, str], str] = {}
    domains: dict[str, str] = {}
    brands: dict[str, str] = {}
    follow_up_only: dict[str, str] = {}

    def out(detail: dict[str, Any]) -> dict[str, Any]:
        detail.update(name_keys=len(names), network_pairs=len(pairs),
                      domains=len(domains), brand_keys=len(brands),
                      follow_up_only=sorted(follow_up_only))
        return {"names": names, "pairs": pairs, "domains": domains,
                "brands": brands, "follow_up_only": follow_up_only,
                "leg": detail}

    # ROW identities are folded FIRST, and the bare name/id lists only as a
    # fallback. `setdefault` means whichever fold runs first owns the citation,
    # and the row fold is the one that can say WHICH row and WHICH status - the
    # difference between "an engaged row carries this program name" and
    # "row 32 `Eden` is already `partnership_form_submitted_awaiting_response`".
    # The second sentence is the one an operator can act on, and it is also the
    # one `follow_up_only` reads to decide whether a door is a duplicate or an
    # unanswered outreach.
    if sheet_index:
        _engaged_row_identities(sheet_index.get("engaged_rows") or (), "governed Sheet",
                                names, pairs, domains, brands, collisions,
                                follow_up_only)
        for key in sheet_index.get("engaged_names") or ():
            names.setdefault(str(key), "governed Sheet: an engaged row carries this program name")
        for network, program_id in sheet_index.get("engaged_ids") or ():
            pairs.setdefault((norm(network), str(program_id).casefold()),
                             f"governed Sheet: engaged row {network}:{program_id}")
        return out({"ok": True, "source": "live_read"})

    mirror = read_sheet_mirror(mirror_path)
    if not mirror:
        return out({"ok": False, "source": "none",
                    "why": "no live Sheet read was supplied and no mirror exists"})
    _engaged_row_identities(mirror.get("engaged_rows") or (), "governed Sheet mirror",
                            names, pairs, domains, brands, collisions, follow_up_only)
    for key in mirror.get("engaged_names") or ():
        names.setdefault(str(key), "governed Sheet mirror: an engaged row carries this program name")
    for encoded in mirror.get("engaged_ids") or ():
        network, _, program_id = str(encoded).partition("\t")
        if program_id:
            pairs.setdefault((norm(network), program_id.casefold()),
                             f"governed Sheet mirror: engaged row {network}:{program_id}")
    return out({"ok": True, "source": "mirror",
                "captured_at_utc": mirror.get("captured_at_utc"),
                "schema": mirror.get("schema")})


# ---------------------------------------------------------------------------
# Leg 5: the relationship suppression ledger
# ---------------------------------------------------------------------------


def suppression_identities(path: Path | None = None) -> dict[str, Any]:
    """Counterparties the operator or a network has already ended.

    Re-applying to a brand we were rejected by, or that the operator fired, is
    the same defect as re-applying to one we already have.
    """
    path = Path(path or SUPPRESSION_LEDGER)
    names: dict[str, str] = {}
    pairs: dict[tuple[str, str], str] = {}
    domains: dict[str, str] = {}
    brands: dict[str, str] = {}
    try:
        entries = (json.loads(path.read_text()).get("entries") or {})
    except (OSError, ValueError) as exc:
        return {"names": names, "pairs": pairs, "domains": domains, "brands": brands,
                "leg": {"ok": False, "why": f"{type(exc).__name__}:{exc}"[:160]}}
    for key, entry in entries.items():
        title = str(entry.get("advertiser_name") or entry.get("campaign_title") or "").strip()
        network = str(entry.get("network") or "").strip()
        reason = str(entry.get("reason") or "suppressed")[:160]
        cite = (f"relationship is SUPPRESSED (`{key}`"
                + (f", {network}" if network else "") + f"): {reason}")
        for spelling in (title, entry.get("campaign_title")):
            spelled = norm(spelling)
            if spelled:
                names.setdefault(spelled, cite)
            fuzzy = brand_key(spelling)
            if fuzzy and fuzzy != spelled:
                brands.setdefault(fuzzy, cite)
        campaign = str(entry.get("campaign_id") or "").strip().casefold()
        if network and campaign:
            pairs.setdefault((norm(network), campaign), cite)
        host = domain_key(entry.get("destination"))
        if host:
            domains.setdefault(host, f"{cite} (merchant domain {host})")
    return {"names": names, "pairs": pairs, "domains": domains, "brands": brands,
            "leg": {"ok": True, "entries": len(entries), "name_keys": len(names),
                    "domains": len(domains), "brand_keys": len(brands)}}


# ---------------------------------------------------------------------------
# Legs 3 + 4: the applications ledger and the sheet-writer queue
# ---------------------------------------------------------------------------


def ledger_identities(path: Path | None = None) -> dict[str, Any]:
    """Every program the executor ledger has ever touched, in any state."""
    path = Path(path or LEDGER_PATH)
    names: dict[str, str] = {}
    pairs: dict[tuple[str, str], str] = {}
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {"names": names, "pairs": pairs,
                "leg": {"ok": False, "why": f"{type(exc).__name__}:{exc}"[:160]}}
    domains: dict[str, str] = {}
    brands: dict[str, str] = {}
    rows = document.get("applications") or []
    for row in rows:
        cite = (f"applications ledger holds `{row.get('name')}` on "
                f"{row.get('network')} (status {row.get('status')})")
        name_key = norm(row.get("name"))
        if name_key:
            names.setdefault(name_key, cite)
        fuzzy = brand_key(row.get("name"))
        if fuzzy and fuzzy != name_key:
            brands.setdefault(fuzzy, cite)
        network = norm(row.get("network"))
        program_id = str(row.get("program_id") or "").strip().casefold()
        if network and program_id:
            pairs.setdefault((network, program_id), cite)
        for url in (row.get("url"), row.get("application_url")):
            host = domain_key(url)
            if host:
                domains.setdefault(host, f"{cite} (merchant domain {host})")
    return {"names": names, "pairs": pairs, "domains": domains, "brands": brands,
            "leg": {"ok": True, "rows": len(rows), "name_keys": len(names),
                    "network_pairs": len(pairs), "domains": len(domains)}}


def queue_identities(path: Path | None = None,
                     queue_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Programs already handed to the ONE Sheet writer.

    Two sources, both optional: the local `queued-index.json` the applications
    machine keeps, and (when supplied by the caller) live `sheet_update_queue`
    rows. n8n is not reachable from every lane, so the datatable is never read
    from in here — a caller that has it passes it in.
    """
    names: dict[str, str] = {}
    pairs: dict[tuple[str, str], str] = {}
    detail: dict[str, Any] = {"ok": True, "queued_index": 0, "queue_rows": 0}
    path = Path(path or QUEUED_INDEX)
    if path.is_file():
        try:
            entries = (json.loads(path.read_text()).get("entries") or {})
        except (OSError, ValueError):
            entries = {}
            detail["queued_index_error"] = "unreadable"
        detail["queued_index"] = len(entries)
        for key, entry in entries.items():
            network, _, program_id = str(key).partition(":")
            if network and program_id:
                pairs.setdefault((norm(network), program_id.casefold()),
                                 f"already queued to the Sheet writer as `{key}`")
            name_key = norm((entry or {}).get("name") if isinstance(entry, dict) else "")
            if name_key:
                names.setdefault(name_key, f"already queued to the Sheet writer as `{key}`")
    for row in queue_rows or []:
        detail["queue_rows"] += 1
        name_key = norm(row.get("program_name"))
        if name_key:
            names.setdefault(name_key, f"sheet_update_queue row `{row.get('update_id')}` "
                                       f"({row.get('kind')})")
        network = norm(row.get("network"))
        program_id = str(row.get("program_id") or "").strip().casefold()
        if network and program_id:
            pairs.setdefault((network, program_id),
                             f"sheet_update_queue row `{row.get('update_id')}` ({row.get('kind')})")
    return {"names": names, "pairs": pairs, "domains": {}, "brands": {}, "leg": detail}


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class DedupeIndex:
    """Answers `verdict()` and shows its work."""

    def __init__(self) -> None:
        self.names: dict[str, dict[str, str]] = {}          # key -> {leg: evidence}
        self.pairs: dict[tuple[str, str], dict[str, str]] = {}
        self.domains: dict[str, dict[str, str]] = {}
        self.brands: dict[str, dict[str, str]] = {}
        self.collisions: dict[str, list[dict[str, Any]]] = {}
        self.legs: dict[str, dict[str, Any]] = {}
        #: name key -> the unanswered-outreach statuses that are its ONLY hit.
        self.follow_up_only: dict[str, str] = {}
        self.built_at_utc: str = utc_now()

    # -- construction -----------------------------------------------------
    def add(self, leg: str, names: dict[str, str], pairs: dict[tuple[str, str], str],
            detail: dict[str, Any], domains: dict[str, str] | None = None,
            brands: dict[str, str] | None = None,
            follow_up_only: dict[str, str] | None = None) -> None:
        for key, evidence in names.items():
            self.names.setdefault(key, {}).setdefault(leg, evidence)
        for key, evidence in pairs.items():
            self.pairs.setdefault(key, {}).setdefault(leg, evidence)
        for key, evidence in (domains or {}).items():
            self.domains.setdefault(key, {}).setdefault(leg, evidence)
        for key, evidence in (brands or {}).items():
            self.brands.setdefault(key, {}).setdefault(leg, evidence)
        for key, statuses in (follow_up_only or {}).items():
            self.follow_up_only.setdefault(key, statuses)
        self.legs[leg] = detail

    # -- the question -----------------------------------------------------
    def verdict(self, name: str, network: str, program_id: str,
                *, aliases: Iterable[str] = (), urls: Iterable[str] = (),
                legs: Iterable[str] = ALL_LEGS,
                allow_follow_up: bool = False) -> DedupeSkip | None:
        """`None` means clear to apply. Anything else means STOP, with a reason.

        Ordered so the most authoritative answer wins the citation: the site is
        what we actually earn from, then the governed Sheet, then our own
        records of having acted.

        `legs` narrows which stores may answer. The executor drain passes
        everything EXCEPT `ledger`, because the row it is asking about is itself
        in the ledger — the drain has its own ledger replay guard and does not
        need this one to tell it about itself.

        `allow_follow_up` is the ONE opt-in narrowing, and it is deliberately
        weak: it releases a name key ONLY when the sheet leg says every engaged
        row behind that key is a `FOLLOW_UP_ELIGIBLE_STATUSES` outreach with no
        provider answer, and only for the sheet leg. The site leg, the
        suppression ledger, the applications ledger, the queue, the network-pair
        axis and the domain axis all still bite, so a real relationship on ANY
        other store still stops the caller.
        """
        network_key = norm(network)
        program_key = str(program_id or "").strip().casefold()
        pair_hit = self.pairs.get((network_key, program_key)) if network_key and program_key else None
        spellings = [name, program_id, *aliases]
        name_keys = [key for key in dict.fromkeys(norm(value) for value in spellings) if key]
        domain_keys = [key for key in dict.fromkeys(domain_key(value) for value in urls) if key]
        brand_keys = [key for key in dict.fromkeys(brand_key(value) for value in spellings)
                      if key and key not in name_keys]
        wanted = tuple(leg for leg in ALL_LEGS if leg in set(legs))

        # ---- tier 1: CERTAIN. An exact identity match on any axis, on ANY
        # network, is an existing relationship. Blocks outright.
        for leg in wanted:
            if pair_hit and leg in pair_hit:
                return _skip(leg, _REASONS[leg]["pair"], f"{network}:{program_id}", pair_hit[leg])
            for key in name_keys:
                hit = self.names.get(key)
                if hit and leg in hit:
                    if (allow_follow_up and leg == LEG_SHEET
                            and key in self.follow_up_only):
                        continue
                    return _skip(leg, _REASONS[leg]["name"], key, hit[leg])
            for key in domain_keys:
                hit = self.domains.get(key)
                if hit and leg in hit:
                    return _skip(leg, _REASONS[leg]["domain"], key, hit[leg])

        # ---- tier 2: AMBIGUOUS. The suffix-stripped brand matches something we
        # already have, but no exact axis does. That is not proof of the same
        # company, so we neither merge nor apply: we skip and ask for a human
        # identity ruling. Safe-by-default is the whole point — applying twice
        # to one brand is the failure we are here to stop.
        for leg in wanted:
            for key in brand_keys:
                hit = self.brands.get(key)
                if hit and leg in hit:
                    if (allow_follow_up and leg == LEG_SHEET
                            and key in self.follow_up_only):
                        continue
                    return _skip(leg, "needs_identity_review", key,
                                 f"{hit[leg]} — brand key `{key}` matches, but no exact "
                                 f"name, program id or domain does. Not applied; a human "
                                 f"must rule whether these are the same company.")
        return None

    def collision_report(self) -> list[dict[str, Any]]:
        """Brands the governed Sheet holds more than once. Reported, never merged."""
        return [{"brand_key": key, "rows": rows}
                for key, rows in sorted(self.collisions.items())]

    # -- receipt ----------------------------------------------------------
    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "glp.application_dedupe.index.v1",
            "built_at_utc": self.built_at_utc,
            "legs": self.legs,
            "name_keys": len(self.names),
            "network_pairs": len(self.pairs),
            "domains": len(self.domains),
            "brand_keys": len(self.brands),
            "follow_up_only_keys": sorted(self.follow_up_only),
            "sheet_brand_collisions": self.collision_report(),
        }


_REASONS = {
    LEG_SITE: {
        "name": "already_live_on_site",
        "pair": "already_live_on_site_by_network_id",
        "domain": "already_live_on_site_by_domain",
    },
    LEG_SHEET: {
        "name": "sheet_already_engaged",
        "pair": "sheet_already_engaged_by_program_id",
        "domain": "sheet_already_engaged_by_domain",
    },
    LEG_SUPPRESSED: {
        "name": "relationship_suppressed",
        "pair": "relationship_suppressed",
        "domain": "relationship_suppressed_by_domain",
    },
    LEG_LEDGER: {
        "name": "already_in_applications_ledger",
        "pair": "already_in_applications_ledger",
        "domain": "already_in_applications_ledger_by_domain",
    },
    LEG_QUEUE: {
        "name": "already_queued_to_sheet_writer",
        "pair": "already_queued_to_sheet_writer",
        "domain": "already_queued_to_sheet_writer_by_domain",
    },
}


def build(*, sheet_index: dict[str, Any] | None = None,
          queue_rows: list[dict[str, Any]] | None = None,
          ledger_path: Path | None = None,
          queued_index_path: Path | None = None,
          sheet_mirror_path: Path | None = None,
          suppression_path: Path | None = None,
          check_live_manifest: bool = False,
          require: Iterable[str] = (LEG_SITE,)) -> DedupeIndex:
    """Build the index. The site leg is mandatory and raises if unreadable."""
    index = DedupeIndex()

    def fold(leg: str, produced: dict[str, Any]) -> None:
        index.add(leg, produced["names"], produced["pairs"], produced["leg"],
                  produced.get("domains"), produced.get("brands"),
                  produced.get("follow_up_only"))

    try:
        fold(LEG_SITE, site_identities(check_live_manifest=check_live_manifest))
    except Exception as exc:  # noqa: BLE001
        index.legs[LEG_SITE] = {"ok": False, "why": f"{type(exc).__name__}:{exc}"[:200]}

    fold(LEG_SHEET, _sheet_leg(sheet_index, sheet_mirror_path, index.collisions))
    fold(LEG_SUPPRESSED, suppression_identities(suppression_path))
    fold(LEG_LEDGER, ledger_identities(ledger_path))
    fold(LEG_QUEUE, queue_identities(queued_index_path, queue_rows))

    missing = [leg for leg in require if not (index.legs.get(leg) or {}).get("ok")]
    if missing:
        raise RuntimeError(
            "application_dedupe_leg_unavailable:" + ",".join(missing) + " :: "
            + json.dumps({leg: index.legs.get(leg) for leg in missing}, sort_keys=True))
    return index


def _cli(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs=3, metavar=("NAME", "NETWORK", "PROGRAM_ID"),
                        action="append", default=[],
                        help="ask the index about one candidate; repeatable")
    parser.add_argument("--live-manifest", action="store_true",
                        help="also fetch the deployed /go/_manifest")
    parser.add_argument("--sheet", action="store_true",
                        help="read the governed Sheet live (needs googleapiclient)")
    args = parser.parse_args(argv)

    sheet_index = None
    if args.sheet:
        sys.path.insert(0, str(SCRIPTS))
        import glp_application_machine as machine  # noqa: E402
        values = machine.read_sheet()
        sheet_index = machine.sheet_index(values)
        write_sheet_mirror(sheet_index["engaged_names"], sheet_index["engaged_ids"],
                           row_count=sheet_index["row_count"])

    index = build(sheet_index=sheet_index, check_live_manifest=args.live_manifest)
    out: dict[str, Any] = {"index": index.receipt(), "checks": []}
    for name, network, program_id in args.check:
        verdict = index.verdict(name, network, program_id)
        out["checks"].append({
            "name": name, "network": network, "program_id": program_id,
            "skip": bool(verdict), **(dict(verdict) if verdict else {}),
        })
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
