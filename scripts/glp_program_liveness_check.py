#!/usr/bin/env python3
"""Weekly liveness audit for every affiliate program live on glpclinicscout.com.

WHY THIS EXISTS
---------------
2026-08-17: Katalys paused three programs we had live. We found out because a
notification email happened to be read. That is not a machine - it is luck. The
operator's bar is: *we never have programs up on our site that are no longer
doing affiliate programs.* So this asks the question on a schedule instead of
waiting to be told.

WHAT IT DOES
------------
1. Loads the program set the SITE actually serves - the current release pointer
   (reports/glp-clinic-scout/seo-machine/current-content-base.json) -> release
   root -> functions/go/program-manifest.js + data/program-registry.json - and
   cross-checks it against the deployed https://glpclinicscout.com/go/_manifest.
2. Maps each program to its source network and network program/offer id, from
   the release's own identity files (activation selection, held-link manifest,
   wire-in approvals, approved-program registry, worker manifest).
3. Asks each network whether the program is still running:
     * Awin      - API, joined-programmes list + programme status  (no click)
     * CJ        - API, advertiser-lookup joined + account-status   (no click)
     * HealthTrader - API, offers/list (browser UA: the WAF 403s urllib)
     * Katalys/RevOffers and other portal-only rails - UNKNOWN until a
       publisher relationship can be checked without opening tracking links.
4. Writes a JSON receipt and a REMOVAL QUEUE of programs that need to come off
   the site. It never edits the site: removal is the proven, deliberate
   scripts/glp_site_paused_program_removal.py run.

Exit 0 = the audit ran and produced verdicts (including "some are paused").
Exit non-zero = the audit itself failed and its answer cannot be trusted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
POINTER = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
SECRETS = Path("/home/ubuntu/.hermes/secrets/affiliate-marketplaces")
LIVE_MANIFEST_URL = "https://glpclinicscout.com/go/_manifest"
DEFAULT_QUEUE = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/liveness-removal-queue.json"
RECEIPT_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/liveness"

TRACKING_PROBE_DISCLOSURE = (
    "Tracking-link requests are prohibited. API-unavailable networks remain UNKNOWN "
    "until their publisher relationship can be verified without a click."
)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
TIMEOUT_S = 30

VERDICT_LIVE = "live"
VERDICT_PAUSED = "paused"
VERDICT_GONE = "gone"
VERDICT_UNKNOWN = "unknown"
#: The verdicts that mean a surface on the site is dishonest and has to go.
REMOVAL_VERDICTS = (VERDICT_PAUSED, VERDICT_GONE)


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())



def http_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """One HTTP GET reported as data, never as an exception."""
    request = urllib.request.Request(url, headers=headers or {"User-Agent": BROWSER_UA})
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            body = response.read(200_000)
            return {"ok": True, "status": response.status, "location": None,
                    "body": body.decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(200_000)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "status": exc.code,
                "location": exc.headers.get("Location") if exc.headers else None,
                "body": body.decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "location": None, "body": "",
                "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 1. What the SITE is actually serving
# ---------------------------------------------------------------------------


def release_root() -> Path:
    pointer = json.loads(POINTER.read_text())
    root = Path(pointer["release_root"])
    if not (root / "data/program-registry.json").is_file():
        raise SystemExit(f"release_pointer_not_a_release:{root}")
    return root


def go_manifest_programs(release: Path) -> dict[str, dict[str, Any]]:
    """Parse the generated /go runtime manifest module - what the worker routes."""
    source = (release / "functions/go/program-manifest.js").read_text()
    match = re.search(r"GO_RUNTIME_MANIFEST\s*=\s*Object\.freeze\((\{.*\})\);", source, re.S)
    if not match:
        raise SystemExit("go_runtime_manifest_unparseable")
    manifest = json.loads(match.group(1))
    return {"manifest_sha256": manifest.get("manifest_sha256"),
            "programs": manifest.get("programs") or {}}


def registry_live_programs(release: Path, profile: str = "glp-clinic-scout") -> dict[str, dict]:
    registry = json.loads((release / "data/program-registry.json").read_text())
    live = {}
    for row in registry["programs"]:
        block = (row.get("profiles", {}) or {}).get(profile, {}) or {}
        surfaces = block.get("surfaces") or {}
        if row.get("commercial_state") == "live" and block.get("wired") is True and (
                surfaces.get("go") is True or surfaces.get("review") is True):
            live[str(row["program_id"])] = {
                "display_name": row.get("display_name"),
                "review_path": row.get("review_path"),
                "route": (row.get("go") or {}).get("route"),
                "source_kind": row.get("source_kind"),
                "surfaces": surfaces,
            }
    return live


# ---------------------------------------------------------------------------
# 2. Program -> network identity
# ---------------------------------------------------------------------------


def identity_map(release: Path) -> dict[str, dict[str, Any]]:
    """Resolve every site program to (network, network program id, tracking url).

    Deliberately multi-source and first-writer-wins in confidence order, because
    the identity lives in whichever file the lane that wired the program used.
    """
    data = release / "data"
    mapping: dict[str, dict[str, Any]] = {}

    def put(key: Any, network: Any, program_id: Any, tracking: Any, source: str) -> None:
        key = str(key or "").strip()
        if not key or key in mapping:
            return
        mapping[key] = {"network": str(network or "").strip(),
                        "network_program_id": str(program_id or "").strip(),
                        "tracking_url": str(tracking or "").strip() or None,
                        "identity_source": source}

    def load(name: str) -> dict[str, Any]:
        path = data / name
        return json.loads(path.read_text()) if path.is_file() else {}

    for row in load("affiliate-activation-selection.json").get("programs", []):
        put(row.get("program_id"), row.get("network"), row.get("network_program_id"),
            None, "affiliate-activation-selection.json")

    for row in load("affiliate-wire-in-approvals.json").get("programs", []):
        tracking = (row.get("tracking") or {}).get("provider_issued_url")
        for key in (row.get("existing_release_alias"), row.get("display_name")):
            put(key, row.get("network"), row.get("provider_program_id"), tracking,
                "affiliate-wire-in-approvals.json")

    for row in load("affiliate-held-link-manifest.json").get("programs", []):
        for key in (row.get("content_program_id"), row.get("worker_program_id")):
            put(key, row.get("network"), row.get("immutable_network_program_id"), None,
                "affiliate-held-link-manifest.json")

    for row in load("approved-program-registry.json").get("programs", []):
        tracking = (row.get("tracking") or {}).get("provider_issued_url")
        for key in (row.get("registry_id"), row.get("existing_release_alias"),
                    row.get("display_name")):
            put(key, row.get("network"), row.get("provider_program_id"), tracking,
                "approved-program-registry.json")

    for row in load("worker-manifest.staging.json").get("programs", []):
        tracking = row.get("tracking")
        put(row.get("program_id"), row.get("network"), None,
            tracking if isinstance(tracking, str) else None, "worker-manifest.staging.json")

    for row in load("program-content-ledger.json").get("programs", []):
        put(row.get("program_id"), row.get("affiliate_network"), None, None,
            "program-content-ledger.json")

    # Name-keyed fallback: the fastpath/batch lanes key by display name.
    for key in list(mapping):
        mapping.setdefault(norm(key), mapping[key])
    return mapping


def resolve_identity(program_id: str, display_name: str | None,
                     mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for key in (program_id, norm(program_id), display_name, norm(display_name)):
        if key and key in mapping and mapping[key].get("network"):
            return dict(mapping[key])
    return {"network": "", "network_program_id": "", "tracking_url": None,
            "identity_source": None}



# ---------------------------------------------------------------------------
# 3. Network adapters
# ---------------------------------------------------------------------------


def secret(name: str, key: str) -> str | None:
    path = SECRETS / name / "api.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get(key)
    except Exception:  # noqa: BLE001
        return None


class NetworkState:
    """Per-run cache of each network's joined-programme catalogue."""

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.errors: dict[str, str] = {}
        self._awin: dict[str, dict] | None = None
        self._cj: dict[str, dict] | None = None
        self._healthtrader: dict[str, dict] | None = None
        self.probe_clicks = 0

    # -- Awin ---------------------------------------------------------------
    def awin(self) -> dict[str, dict] | None:
        if self._awin is not None or self.offline:
            return self._awin
        token = secret("awin", "api_key")
        if not token:
            self.errors["awin"] = "no_api_key_on_disk"
            return None
        headers = {"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA}
        accounts = http_get("https://api.awin.com/accounts", headers)
        publisher_id = None
        if accounts["ok"] and accounts["status"] == 200:
            try:
                for account in json.loads(accounts["body"]).get("accounts", []):
                    if account.get("accountType") == "publisher":
                        publisher_id = account["accountId"]
                        break
            except Exception:  # noqa: BLE001
                publisher_id = None
        if not publisher_id:
            self.errors["awin"] = f"accounts_lookup_failed:{accounts.get('status')}"
            return None
        response = http_get(
            f"https://api.awin.com/publishers/{publisher_id}/programmes?relationship=joined",
            headers)
        if not response["ok"] or response["status"] != 200:
            self.errors["awin"] = f"programmes_http_{response.get('status')}"
            return None
        try:
            rows = json.loads(response["body"])
        except Exception as exc:  # noqa: BLE001
            self.errors["awin"] = f"programmes_unparseable:{exc}"
            return None
        self._awin = {str(row["id"]): row for row in rows if row.get("id") is not None}
        return self._awin

    # -- CJ -----------------------------------------------------------------
    def cj(self) -> dict[str, dict] | None:
        if self._cj is not None or self.offline:
            return self._cj
        token = secret("cj", "personal_access_token")
        cid = secret("cj", "requestor_cid")
        if not token or not cid:
            self.errors["cj"] = "no_credentials_on_disk"
            return None
        response = http_get(
            "https://advertiser-lookup.api.cj.com/v2/advertiser-lookup"
            f"?requestor-cid={urllib.parse.quote(str(cid))}&advertiser-ids=joined",
            {"Authorization": f"Bearer {token}", "Accept": "application/xml",
             "User-Agent": BROWSER_UA})
        if not response["ok"] or response["status"] != 200:
            self.errors["cj"] = f"advertiser_lookup_http_{response.get('status')}"
            return None
        try:
            root = ET.fromstring(response["body"])
        except ET.ParseError as exc:
            self.errors["cj"] = f"advertiser_lookup_unparseable:{exc}"
            return None
        catalogue = {}
        for advertiser in root.iter("advertiser"):
            aid = advertiser.findtext("advertiser-id")
            if aid:
                catalogue[str(aid).strip()] = {
                    "account_status": (advertiser.findtext("account-status") or "").strip(),
                    "relationship_status": (advertiser.findtext("relationship-status") or "").strip(),
                    "advertiser_name": (advertiser.findtext("advertiser-name") or "").strip(),
                }
        self._cj = catalogue
        return self._cj

    # -- HealthTrader -------------------------------------------------------
    def healthtrader(self) -> dict[str, dict] | None:
        """Offers list. The WAF 403s python-urllib's default UA - send a browser one."""
        if self._healthtrader is not None or self.offline:
            return self._healthtrader
        token_path = SECRETS / "healthtrader/access_token.json"
        if not token_path.is_file():
            self.errors["healthtrader"] = "no_access_token_on_disk"
            return None
        try:
            token = json.loads(token_path.read_text()).get("access_token")
        except Exception as exc:  # noqa: BLE001
            self.errors["healthtrader"] = f"token_unreadable:{exc}"
            return None
        response = http_get(
            "https://api.healthtrader.com/affiliates/v1/offers/list",
            {"Authorization": f"Bearer {token}", "Accept": "application/json",
             "User-Agent": BROWSER_UA})
        if not response["ok"] or response["status"] != 200:
            self.errors["healthtrader"] = f"offers_http_{response.get('status')}"
            return None
        try:
            payload = json.loads(response["body"])
        except Exception as exc:  # noqa: BLE001
            self.errors["healthtrader"] = f"offers_unparseable:{exc}"
            return None
        offers = payload.get("data") or payload.get("offers") or payload
        catalogue = {}
        if isinstance(offers, list):
            for offer in offers:
                key = str(offer.get("unique_id") or offer.get("id") or "").strip()
                if key:
                    catalogue[key] = offer
        self._healthtrader = catalogue
        return self._healthtrader


def check_katalys(identity: dict[str, Any], state: NetworkState) -> dict[str, Any]:
    return check_portal_only(identity, state)


def check_awin(identity: dict[str, Any], state: NetworkState) -> dict[str, Any]:
    if state.offline:
        return {"verdict": VERDICT_UNKNOWN, "check": "offline",
                "evidence": "offline mode: Awin API not queried"}
    catalogue = state.awin()
    if catalogue is None:
        return {"verdict": VERDICT_UNKNOWN, "check": "awin_api",
                "evidence": f"Awin API unavailable: {state.errors.get('awin')}"}
    key = str(identity.get("network_program_id") or "")
    row = catalogue.get(key)
    if not key:
        return {"verdict": VERDICT_UNKNOWN, "check": "awin_api",
                "evidence": "no Awin advertiser id on file for this program"}
    if row is None:
        return {"verdict": VERDICT_GONE, "check": "awin_api",
                "evidence": f"advertiser {key} is not in our joined-programmes list "
                            f"({len(catalogue)} joined)"}
    status = str(row.get("status") or "")
    if status.lower() == "active":
        return {"verdict": VERDICT_LIVE, "check": "awin_api",
                "evidence": f"joined programme {key} ({row.get('name')}) status={status}"}
    return {"verdict": VERDICT_PAUSED, "check": "awin_api",
            "evidence": f"joined programme {key} ({row.get('name')}) status={status}"}


def check_cj(identity: dict[str, Any], state: NetworkState) -> dict[str, Any]:
    if state.offline:
        return {"verdict": VERDICT_UNKNOWN, "check": "offline",
                "evidence": "offline mode: CJ API not queried"}
    catalogue = state.cj()
    if catalogue is None:
        return {"verdict": VERDICT_UNKNOWN, "check": "cj_api",
                "evidence": f"CJ API unavailable: {state.errors.get('cj')}"}
    key = str(identity.get("network_program_id") or "")
    if not key:
        return {"verdict": VERDICT_UNKNOWN, "check": "cj_api",
                "evidence": "no CJ advertiser id on file for this program"}
    row = catalogue.get(key)
    if row is None:
        return {"verdict": VERDICT_GONE, "check": "cj_api",
                "evidence": f"advertiser {key} is not in our joined list ({len(catalogue)} joined)"}
    if row["account_status"].lower() == "active" and row["relationship_status"].lower() == "joined":
        return {"verdict": VERDICT_LIVE, "check": "cj_api",
                "evidence": f"advertiser {key} ({row['advertiser_name']}) "
                            f"account={row['account_status']} relationship={row['relationship_status']}"}
    return {"verdict": VERDICT_PAUSED, "check": "cj_api",
            "evidence": f"advertiser {key} ({row['advertiser_name']}) "
                        f"account={row['account_status']} relationship={row['relationship_status']}"}


def check_healthtrader(identity: dict[str, Any], state: NetworkState) -> dict[str, Any]:
    if state.offline:
        return {"verdict": VERDICT_UNKNOWN, "check": "offline",
                "evidence": "offline mode: HealthTrader API not queried"}
    catalogue = state.healthtrader()
    if catalogue is None:
        return {"verdict": VERDICT_UNKNOWN, "check": "healthtrader_api",
                "evidence": f"HealthTrader API unavailable: {state.errors.get('healthtrader')}"}
    key = str(identity.get("network_program_id") or "")
    if not key:
        return {"verdict": VERDICT_UNKNOWN, "check": "healthtrader_api",
                "evidence": "no HealthTrader offer id on file for this program"}
    if key not in catalogue:
        return {"verdict": VERDICT_GONE, "check": "healthtrader_api",
                "evidence": f"offer {key} is not in the offers/list catalogue "
                            f"({len(catalogue)} offers)"}
    return {"verdict": VERDICT_LIVE, "check": "healthtrader_api",
            "evidence": f"offer {key} present in offers/list"}


def check_portal_only(identity: dict[str, Any], state: NetworkState) -> dict[str, Any]:
    """No publisher API: preserve uncertainty without opening tracking links."""
    network = identity.get("network") or "unmapped"
    return {"verdict": VERDICT_UNKNOWN, "check": "portal_check_needed",
            "evidence": f"{network}: no available publisher relationship API; "
                        "confirm the program in the portal without opening a tracking link"}


NETWORK_ADAPTERS = {
    "katalys": check_katalys,
    "revoffers": check_katalys,
    "awin": check_awin,
    "cj affiliate": check_cj,
    "cj": check_cj,
    "healthtrader": check_healthtrader,
}


def snapshot_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index = {}
    for row in rows or []:
        network = norm(row.get("network"))
        program_id = str(row.get("program_id") or "").strip()
        if network and program_id:
            index[(network, program_id)] = row
    return index


def check_program(program: dict[str, Any], state: NetworkState,
                  snapshot: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    identity = program["identity"]
    network = str(identity.get("network") or "").strip()
    adapter = NETWORK_ADAPTERS.get(network.lower(), check_portal_only)
    result = adapter(identity, state)
    corroboration = snapshot.get((norm(network), str(identity.get("network_program_id") or "")))
    if corroboration:
        result = dict(result)
        result["marketplace_snapshot"] = {
            "status": corroboration.get("status"),
            "relationship": corroboration.get("relationship"),
            "last_seen_utc": corroboration.get("last_seen_utc"),
        }
    return result


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit(snapshot_rows: list[dict[str, Any]], offline: bool = False) -> dict[str, Any]:
    release = release_root()
    go = go_manifest_programs(release)
    registry = registry_live_programs(release)
    mapping = identity_map(release)
    state = NetworkState(offline=offline)
    snapshot = snapshot_index(snapshot_rows)

    live_site = {"ok": False, "program_ids": [], "manifest_sha256": None}
    if not offline:
        response = http_get(LIVE_MANIFEST_URL)
        if response["ok"] and response["status"] == 200:
            try:
                payload = json.loads(response["body"])
                live_site = {"ok": True,
                             "program_ids": sorted(payload.get("program_ids") or []),
                             "manifest_sha256": payload.get("manifest_sha256")}
            except Exception as exc:  # noqa: BLE001
                live_site["error"] = f"unparseable:{exc}"
        else:
            live_site["error"] = f"http_{response.get('status')}:{response.get('error', '')}"

    site_ids = sorted(set(go["programs"]) | set(registry) | set(live_site["program_ids"]))
    results = []
    for program_id in site_ids:
        registry_row = registry.get(program_id, {})
        display_name = (registry_row.get("display_name")
                        or (go["programs"].get(program_id) or {}).get("display_name"))
        identity = resolve_identity(program_id, display_name, mapping)
        program = {
            "program": program_id,
            "display_name": display_name,
            "network": identity.get("network") or "unmapped",
            "network_program_id": identity.get("network_program_id") or None,
            "identity_source": identity.get("identity_source"),
            "review_path": registry_row.get("review_path"),
            "go_route": registry_row.get("route") or (go["programs"].get(program_id) or {}).get("route"),
            "on_live_site": program_id in set(live_site["program_ids"]),
            "in_release_go_manifest": program_id in go["programs"],
            "in_release_registry": program_id in registry,
            "identity": identity,
        }
        outcome = check_program(program, state, snapshot)
        program.pop("identity")
        program.update(outcome)
        results.append(program)

    counts: dict[str, int] = {}
    for row in results:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    queue = [row for row in results if row["verdict"] in REMOVAL_VERDICTS]

    manifest_drift = (live_site["ok"] and go.get("manifest_sha256")
                      and live_site["manifest_sha256"] != go["manifest_sha256"])
    return {
        "schema": "glp.program_liveness_audit.v1",
        "checked_at_utc": utc(),
        "release": str(release),
        "site_program_count": len(site_ids),
        "live_site_manifest": live_site,
        "release_manifest_sha256": go.get("manifest_sha256"),
        "live_manifest_matches_release": (not manifest_drift) if live_site["ok"] else None,
        "verdict_counts": counts,
        "programs": results,
        "removal_queue_count": len(queue),
        "network_errors": state.errors,
        "tracking_probe_clicks": state.probe_clicks,
        "tracking_probe_disclosure": TRACKING_PROBE_DISCLOSURE,
        "site_modified": False,
    }


def build_queue(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "glp.program_liveness_removal_queue.v1",
        "generated_at_utc": receipt["checked_at_utc"],
        "release": receipt["release"],
        "note": ("Detection only. Removal is the deliberate, proven run of "
                 "scripts/glp_site_paused_program_removal.py against a fresh release."),
        "programs": [
            {key: row[key] for key in ("program", "display_name", "network",
                                       "network_program_id", "review_path", "go_route",
                                       "verdict", "check", "evidence")}
            for row in receipt["programs"] if row["verdict"] in REMOVAL_VERDICTS
        ],
    }


def load_snapshot_arg(args) -> list[dict[str, Any]]:
    raw = None
    if args.snapshot_file:
        raw = Path(args.snapshot_file).read_text()
    elif args.snapshot_json:
        raw = args.snapshot_json
    if not raw:
        return []
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload.get("rows") or payload.get("programs") or []
    return [row for row in payload if isinstance(row, dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-json", help="Compact [{network,program_id,status}] list from n8n.")
    parser.add_argument("--snapshot-file", help="Same, read from a file.")
    parser.add_argument("--output", help="Receipt path (default: reports/.../liveness/<ts>.json).")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE))
    parser.add_argument("--offline", action="store_true",
                        help="Skip every network call; identity resolution only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Audit release identities offline without HTTP, tracking probes or artifact writes.")
    args = parser.parse_args()

    try:
        snapshot_rows = load_snapshot_arg(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "FAIL", "reason": f"snapshot_unparseable:{exc}"}))
        return 1

    receipt = audit(snapshot_rows, offline=args.offline or args.dry_run)
    receipt["marketplace_snapshot_rows_supplied"] = len(snapshot_rows)

    out = None
    queue_path = None
    if not args.dry_run:
        out = Path(args.output) if args.output else (
            RECEIPT_DIR / f"liveness-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")

        queue_path = Path(args.queue)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = build_queue(receipt)
        queue["receipt_path"] = str(out)
        queue_path.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n")

    counts = receipt["verdict_counts"]
    summary = {
        "status": "PASS",
        "schema": "glp.program_liveness_summary.v1",
        "dry_run": args.dry_run,
        "no_effects": args.dry_run,
        "checked": receipt["site_program_count"],
        "live": counts.get(VERDICT_LIVE, 0),
        "paused": counts.get(VERDICT_PAUSED, 0),
        "gone": counts.get(VERDICT_GONE, 0),
        "unknown": counts.get(VERDICT_UNKNOWN, 0),
        "removal_queue_count": receipt["removal_queue_count"],
        "attention": [
            # display_name + network_program_id are here so the sheet-sync
            # queue can resolve the Sheet row this verdict is about. The site
            # slug alone never matches the Sheet's program name.
            {"program": row["program"], "network": row["network"],
             "display_name": row.get("display_name") or row["program"],
             "network_program_id": row.get("network_program_id") or "",
             "verdict": row["verdict"], "evidence": row["evidence"][:220]}
            for row in receipt["programs"] if row["verdict"] != VERDICT_LIVE
        ],
        "network_errors": receipt["network_errors"],
        "tracking_probe_clicks": receipt["tracking_probe_clicks"],
        "live_manifest_matches_release": receipt["live_manifest_matches_release"],
        "site_modified": False,
        "receipt": str(out) if out else None,
        "queue": str(queue_path) if queue_path else None,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
