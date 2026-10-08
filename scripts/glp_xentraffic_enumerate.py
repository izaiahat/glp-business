#!/usr/bin/env python3
"""The XenTraffic lane of the daily marketplace-enumeration machine.

WHAT IT IS
----------
`glp/marketplace-enumeration` (n8n `iOJhvTtehlHGLUSs`) enumerates ten networks
with n8n-native HTTP nodes because every one of them has an API key. XenTraffic
has none and cannot be given one (the tenant returns an empty `api_key` and
exposes no mint control), so its catalogue — 381 offers, 16 of them explicitly
GLP-1, the best paying at $270 CPA — was invisible to the daily machine and to
every downstream lane.

* `xentraffic_enumerate` now requires a caller-owned fresh AI-browser reader.
  The historical stored-session route is refused before any access-file read.
  It prints one JSON line; missing browser access is UNKNOWN, not zero supply.

WHAT IT DETECTS, AUTOMATICALLY, EVERY DAY
-----------------------------------------
  new_offer          an offer that was not in the catalogue yesterday
  offer_disappeared  one that is gone
  status_change      active <-> paused (the pause detection this rail lacked)
  payout_change      the CPA moved

The comparison baseline is a state file on this box, written only after a
COMPLETE enumeration. An incomplete read never overwrites state — otherwise a
half-answer becomes tomorrow's truth and every real offer looks "disappeared".

WHAT IT REFUSES TO DO
---------------------
* It never writes the Sheet. Detections leave as `sheet_update_queue` rows for
  the one governed writer, exactly like every other detector.
* It never clicks a tracking link.
* It never resets, changes, or types a password. An expired session is reported
  as `SESSION_EXPIRED`, loudly, and is NOT reported as "no offers".

    python3 scripts/glp_xentraffic_enumerate.py                    # list + diff
    python3 scripts/glp_xentraffic_enumerate.py --detail candidates # + fit detail
    python3 scripts/glp_xentraffic_enumerate.py --heartbeat 1       # + n8n heartbeat
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/home/ubuntu/business")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
sys.path.insert(0, str(SOURCE_ROOT / "ops"))
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))

from scripts.marketplace_adapters import xentraffic as xt  # noqa: E402

SCHEMA = "glp.xentraffic_enumerate.v1"
MACHINE = "xentraffic_enumeration"
RUN_ROOT = ROOT / "reports/glp-clinic-scout/affiliate-machine/xentraffic"
STATE_PATH = RUN_ROOT / "state.json"
HEARTBEATS = "9Gevk0AdC7J6UbFn"
REGISTRY = "ReulkaY4kEwCh9RY"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def repo_rel(path: Path) -> str:
    return str(Path(path).resolve().relative_to(ROOT))


def write_private(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(text)
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"schema": SCHEMA + ".state", "offers": [], "captured_at_utc": ""}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"schema": SCHEMA + ".state", "offers": [], "captured_at_utc": ""}


# ---------------------------------------------------------------------------
# Queue rows: what a DIFF may honestly say to the governed writer
# ---------------------------------------------------------------------------
#
# Only ONE delta class maps to a governed mode without new research: a paused /
# disappeared offer we have already put on the Sheet (`network_paused`). A new
# offer needs a researched row (`add_approved_row` demands category, official
# url, fit reason, vertical) — that is the ingest lane's job, not the diff's, so
# the diff surfaces it and stops. Inventing those cells here is the exact
# dishonesty the whole writer architecture exists to prevent.

def queue_rows_for(deltas: list[dict[str, Any]], sheet_program_ids: set[str],
                   run_utc: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queued: list[dict[str, Any]] = []
    surfaced: list[dict[str, Any]] = []
    for delta in deltas:
        offer_id = str(delta.get("offer_id"))
        adverse = (delta["kind"] == xt.DELTA_GONE
                   or (delta["kind"] == xt.DELTA_STATUS
                       and str(delta.get("to") or "").lower() != "active"))
        if adverse and offer_id in sheet_program_ids:
            queued.append({
                "update_id": f"xentraffic-{delta['kind']}-{offer_id}-{run_utc[:10]}",
                "kind": "network_paused",
                "network": xt.NETWORK_ROUTE,
                "program_id": offer_id,
                "program_name": str(delta.get("name") or "")[:160],
                "evidence": json.dumps({
                    "message_id": f"xentraffic-portal-{offer_id}-{run_utc}",
                    "source": "xentraffic authenticated partner portal catalogue",
                    "observed_at_utc": run_utc,
                    "delta": delta,
                }, sort_keys=True),
                "status": "queued",
                "queued_by": MACHINE,
                "queued_at_utc": run_utc,
                "resolved_at_utc": "",
                "note": (f"XenTraffic offer {offer_id} ({delta.get('name')}) went "
                         f"{delta['kind']} in the partner catalogue at {run_utc}: "
                         f"{delta.get('from')} -> {delta.get('to')}.")[:900],
            })
        else:
            surfaced.append(delta)
    return queued, surfaced


def heartbeat(count: int, note: str, receipt: str) -> dict[str, Any]:
    import n8n_dt  # noqa: PLC0415 - only when the caller asks for a heartbeat

    n8n_dt.insert(HEARTBEATS, {
        "machine": MACHINE,
        "last_success_utc": utc_now(),
        "output_count": int(count),
        "receipt": receipt,
        "note": note[:400],
    })
    n8n_dt.upsert(REGISTRY, n8n_dt.eq("machine", MACHINE), {
        "cadence_hours": 24,
        "grace_hours": 8,
        "active_days": "all",
        "owner": "glp/marketplace-enumeration",
        "config": json.dumps({
            "lane": "xentraffic authenticated-session adapter",
            "bridge_action": "xentraffic_enumerate",
            "verify_window_s": 900,
        }, sort_keys=True),
    })
    return {"heartbeat": "inserted", "registry": "present" if existing else "created"}


def coerce_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None, *, fresh_reader=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # The bridge renders EVERY arg as `--key value`, so no `store_true` here.
    ap.add_argument("--detail", nargs="?", const="candidates", default="none",
                    choices=["none", "candidates", "all", "changed"],
                    help="which offers to fetch full detail for (fit gate + links)")
    # OWNERSHIP OF THE QUEUE DECISION. When n8n calls this lane, the parent
    # workflow's snapshot diff is what turns an adverse delta into a
    # `sheet_update_queue` row — the same code path every other network uses. If
    # this script queued as well, one pause would produce TWO queue rows with
    # two update_ids, and the drain dedupes on update_id. So queueing here is
    # opt-in and off by default: exactly one component owns the decision.
    ap.add_argument("--queue", nargs="?", const="on", default="off",
                    choices=["on", "off"])
    ap.add_argument("--heartbeat", nargs="?", const="1", default=None)
    ap.add_argument("--headless", nargs="?", const="1", default="1")
    ap.add_argument("--read-only", choices=('0', '1'), default='0')
    ap.add_argument("--run-id", default="")
    ap.add_argument("--output", default="")
    args = ap.parse_args(argv)

    run_utc = utc_now()
    run_dir = RUN_ROOT / f"run-{args.run_id or stamp()}"
    if (fresh_reader is None or getattr(fresh_reader, 'fresh', False) is not True
            or not getattr(fresh_reader, 'owned_session', '')):
        print(json.dumps({'schema': SCHEMA, 'status': 'BLOCKED',
            'reason': 'fresh_ai_browser_vault_stage_required', 'captured_at_utc': run_utc,
            'rows': [], 'queue_rows': [], 'deltas': [],
            'summary': {'offers': None, 'reported_total': None, 'completeness': 'UNKNOWN'},
            'effects': {'provider_mutations': 0, 'tracking_clicks': 0, 'sheet_direct_writes': 0}}))
        return 2
    if args.read_only == '1' and (args.queue != 'off' or coerce_flag(args.heartbeat)):
        raise SystemExit('xentraffic_read_only_requires_queue_off_and_no_heartbeat')
    previous = load_state()

    try:
        # Caller retains browser ownership and clean-exit responsibility.
        # There is no CLI flag, stored profile or subprocess driver fallback.
        fresh_reader.start()
        session = fresh_reader
        with contextlib.nullcontext(session):
            entries, reported_total = xt.list_offers(session.api)
            offers = [xt.normalize_offer(entry) for entry in entries]
            verdict, note = xt.completeness(len(offers), reported_total)

            deltas = xt.diff_snapshots(previous.get("offers") or [], offers)
            changed = {str(d["offer_id"]) for d in deltas}

            if args.detail == "all":
                wanted = [o["offer_id"] for o in offers]
            elif args.detail == "changed":
                wanted = [o["offer_id"] for o in offers if str(o["offer_id"]) in changed]
            elif args.detail == "candidates":
                # Cheap pre-filter on the list fields, then the real gate on the
                # full catalogue text. A name/category screen keeps the daily
                # detail budget small without deciding anything.
                #
                # `known_fit` is not an optimisation, it is a correctness fix: an
                # offer whose ONLY GLP evidence lives in its description (the
                # BuyXenBurn case) passes the gate on detail and fails the
                # list-level screen. Without carrying its id forward it would
                # drop out of the snapshot the very next day and reappear the day
                # after — a flap the parent diff would report as a real change.
                known_fit = {str(i) for i in (previous.get("fit_offer_ids") or [])}
                # An offer nobody has ever fetched detail for has not been gated
                # at all — only screened. Inspect it once; after that it is in
                # `detailed`, so the steady-state cost is new offers only.
                detailed = {str(i) for i in (previous.get("detailed_offer_ids") or [])}
                wanted = [o["offer_id"] for o in offers
                          if xt.fit_passes(xt.classify_offer(o)["verdict"])
                          or str(o["offer_id"]) in changed
                          or str(o["offer_id"]) in known_fit
                          or str(o["offer_id"]) not in detailed]
            else:
                wanted = []

            details: dict[str, dict[str, Any]] = {}
            for offer_id in wanted:
                detail = xt.offer_detail(session.api, offer_id)
                details[str(offer_id)] = detail
                entry = next(e for e in entries
                             if str(e.get("network_offer_id")) == str(offer_id))
                merged = xt.normalize_offer(entry, detail)
                for index, offer in enumerate(offers):
                    if str(offer["offer_id"]) == str(offer_id):
                        offers[index] = merged
                        break
    except xt.SessionError as exc:
        allowed = {'xentraffic_catalog_total_unknown', 'xentraffic_catalog_total_changed_mid_read',
                   'xentraffic_catalog_page_invalid', 'xentraffic_catalog_duplicate_or_invalid_identity',
                   'xentraffic_catalog_truncated_or_overflow', 'xentraffic_catalog_page_budget_exhausted',
                   'xentraffic_protected_principal_and_partner_unverified',
                   'xentraffic_visible_pagination_not_integrated',
                   'xentraffic_visible_published_total_or_pager_unknown',
                   'xentraffic_visible_offer_table_unknown_or_ambiguous'}
        reason = str(exc) if str(exc) in allowed else 'xentraffic_reader_failed'
        payload = {'schema': SCHEMA, 'status': 'BLOCKED', 'reason': reason,
                   'captured_at_utc': run_utc, 'rows': [], 'queue_rows': [], 'deltas': [],
                   'summary': {'offers': None, 'reported_total': None, 'completeness': 'UNKNOWN'},
                   'effects': {'provider_mutations': 0, 'tracking_clicks': 0, 'sheet_direct_writes': 0}}
        print(json.dumps(payload, sort_keys=True))
        return 2
    except Exception as exc:  # raw browser diagnostics may contain account secrets
        payload = {'schema': SCHEMA, 'status': 'ERROR', 'reason': 'xentraffic_reader_failed',
                   'exception_type': type(exc).__name__, 'captured_at_utc': run_utc,
                   'rows': [], 'queue_rows': [], 'deltas': [],
                   'summary': {'offers': None, 'reported_total': None, 'completeness': 'UNKNOWN'},
                   'effects': {'provider_mutations': 0, 'tracking_clicks': 0, 'sheet_direct_writes': 0}}
        print(json.dumps(payload, sort_keys=True))
        return 2

    for offer in offers:
        gate = xt.classify_offer(offer)
        offer["fit_verdict"] = gate["verdict"]
        offer["fit_reason"] = gate["reason"]
        offer["fit_evidence"] = gate["evidence"]

    core = [o for o in offers if o["fit_verdict"] == xt.VERDICT_CORE]
    sister = [o for o in offers if o["fit_verdict"] == xt.VERDICT_SISTER]
    # Only FIT-PASSING offers are handed to the parent workflow's snapshot. The
    # whole 381-offer catalogue lives in this box's state file, where the diff
    # that detects new supply runs; pushing all of it into `marketplace_snapshot`
    # would blow the workflow's 1,000-row cap and page past n8n's 100-row read
    # limit, which is how a diff silently starts reporting the whole network as
    # brand new. The parent tracks what we could actually wire.
    rows = [xt.snapshot_row(o, run_utc) for o in offers if xt.fit_passes(o["fit_verdict"])]

    if args.queue == "on":
        # A pause is only queueable for a program the Sheet actually carries, so
        # the live Sheet is the authority — not a cached id list that goes stale
        # the moment the drain creates a row.
        import glp_xentraffic_ingest_queue_20260820 as ingest  # noqa: PLC0415

        sheet_ids = ingest.read_sheet_identity()["program_ids"]
        queue_rows, surfaced = queue_rows_for(deltas, sheet_ids, run_utc)
    else:
        queue_rows, surfaced = [], deltas

    capture = write_private(run_dir / "offers.json", {
        "schema": SCHEMA + ".capture",
        "captured_at_utc": run_utc,
        "network": xt.NETWORK,
        "portal_url": xt.PORTAL_URL,
        "enumeration_method": getattr(fresh_reader, 'read_method', 'fresh_ai_browser_vault'),
        "completeness": {"verdict": verdict, "note": note,
                         "enumerated": len(offers), "reported_total": reported_total},
        "offers": offers,
        "raw_detail": details,
    })

    state_updated = False
    if verdict == "COMPLETE" and args.read_only != '1':
        write_private(STATE_PATH, {
            "schema": SCHEMA + ".state",
            "captured_at_utc": run_utc,
            "offers": offers,
            "fit_offer_ids": sorted(str(o["offer_id"]) for o in offers
                                    if xt.fit_passes(o["fit_verdict"])),
            # Carried forward, never reset: an offer inspected once stays
            # inspected, so tomorrow's run only pays for offers that are new.
            "detailed_offer_ids": sorted(
                {str(i) for i in (previous.get("detailed_offer_ids") or [])}
                | set(details)),
            "sheet_program_ids": previous.get("sheet_program_ids") or [],
        })
        state_updated = True

    summary = {
        "offers": len(offers),
        "snapshot_rows": len(rows),
        "reported_total": reported_total,
        "completeness": verdict,
        "core_glp1": len(core),
        "sister_vertical": len(sister),
        "details_fetched": len(details),
        "deltas": len(deltas),
        "queue_rows": len(queue_rows),
        "state_updated": state_updated,
        "note": note,
    }
    payload = {
        "schema": SCHEMA,
        'read_method': 'fresh_ai_browser_vault',
        'effects': {'provider_mutations': 0, 'tracking_clicks': 0, 'sheet_direct_writes': 0},
        "status": "PASS" if verdict == "COMPLETE" else "INCOMPLETE",
        "captured_at_utc": run_utc,
        "capture_path": repo_rel(run_dir / "offers.json"),
        "capture_sha256": capture,
        "rows": rows,
        "queue_rows": queue_rows,
        "deltas": deltas,
        "surfaced_only": surfaced,
        "summary": summary,
    }
    if coerce_flag(args.heartbeat):
        try:
            payload["heartbeat"] = heartbeat(
                len(rows),
                f"{len(offers)} offers ({len(core)} core GLP-1, {len(sister)} sister); "
                f"{len(deltas)} deltas; {verdict}",
                repo_rel(run_dir / "offers.json"))
        except Exception as exc:  # noqa: BLE001 - a missed heartbeat must not lose the read
            payload["heartbeat"] = {"error": f"{type(exc).__name__}: {exc}"}
    if args.output:
        write_private(Path(args.output), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if verdict == "COMPLETE" else 3


if __name__ == "__main__":
    raise SystemExit(main())
