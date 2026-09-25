#!/usr/bin/env python3
"""Fleet-wide OUTPUT health sensor — did each machine PRODUCE, not just run?

Operator mandate 2026-08-15: *"this is ur job to make sure we are running all
systems go and green and healthy... those working is the ONLY way we make
money."* Every failure that week was a job that completed successfully while
accomplishing nothing: the harvester reported 0 uncached while 791 advertisers
sat unfetched, the topic queue drew from a frozen file, qualified Boring
candidates could never become actionable (that lane is retired as of 2026-09-09;
see the note above CHECKS), five approved providers sat without tracking
links. Run-status monitoring can not see any of that. This sensor asks
the only question that matters, per machine: did the expected OUTPUT appear in
the expected window?

Read-only except for one bounded recovery action: observing the SEO article
buffer below floor fires its registered, concurrency-safe refill adapter. Every
other check only reads ledgers/receipts the machines already write.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from article_carrier_gate_health import accepted_failure_health

PT = ZoneInfo("America/Los_Angeles")
BUSINESS = Path("/home/ubuntu/business")
if str(BUSINESS) not in sys.path:
    sys.path.insert(0, str(BUSINESS))
HERMES = Path("/home/ubuntu/.hermes")


def now_pt() -> dt.datetime:
    return dt.datetime.now(PT)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(name: str, ok: bool, expected: str, actual: str) -> dict:
    result = {"machine": name, "ok": bool(ok), "expected": expected, "actual": actual}
    if name == "seo_net_new_article":
        result["accepted_failures"] = accepted_failure_health(last_carrier_day(now_pt().date()).isoformat())
    return result


def last_carrier_day(today: dt.date) -> dt.date:
    day = today
    while day.weekday() not in (0, 2, 4):          # Mon/Wed/Fri
        day -= dt.timedelta(days=1)
    return day


def seo_net_new() -> dict:
    """The most recent Mon/Wed/Fri must have published >=1 announced article."""
    target = last_carrier_day(now_pt().date())
    ledger = read_json(BUSINESS / "reports/glp-clinic-scout/seo-machine/published-route-ledger.json")
    hits = []
    for route, row in (ledger.get("routes") or {}).items():
        seen = str(row.get("seen_at_utc") or "")
        try:
            when = dt.datetime.fromisoformat(seen.replace("Z", "+00:00")).astimezone(PT).date()
        except ValueError:
            continue
        if when == target and row.get("announced"):
            hits.append(route)
    # The carrier runs 11:00 PT and an article takes a while to clear review, so
    # judging the day before ~14:00 reports a machine dead that simply has not
    # run yet. Flagging a not-yet as a failure is the same crying-wolf that
    # makes a real zero-article day easy to scroll past.
    now = now_pt()
    # If the carrier already RAN today and failed, say so immediately — waiting
    # for a clock threshold would hide a real failure for hours. Today the
    # carrier ran 12:09, passed review 5/5, then failed to publish on a
    # playbook-anchor gate; the time-only rule would have stayed silent until
    # 14:00 (bridge, 2026-08-17).
    failure_root = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-carrier-failures"
    # Filenames are UTC; one Pacific day spans two UTC calendar dates.
    if not hits:
        for utc_day in (target, target + dt.timedelta(days=1)):
            for path in sorted(failure_root.glob(f"{utc_day:%Y%m%d}*.json")):
                try:
                    failure = read_json(path)
                    observed = dt.datetime.fromisoformat(str(failure["observed_at_utc"]).replace("Z", "+00:00"))
                    if observed.tzinfo is None:
                        raise ValueError("receipt timezone absent")
                    if observed.astimezone(PT).date() != target:
                        continue
                    if not failure.get("failure_kind") or failure.get("exit_code") == 0:
                        raise ValueError("failure receipt fields invalid")
                except (OSError, KeyError, ValueError, TypeError):
                    return check("seo_net_new_article", False,
                                 f">=1 article published on carrier day {target}",
                                 f"n8n carrier failure evidence UNKNOWN/unreadable: {path.name}")
                return check("seo_net_new_article", False,
                             f">=1 article published on carrier day {target}",
                             f"n8n carrier RAN and FAILED to publish — {failure['failure_kind']}; {path.name}")
    too_early = target == now.date() and now.hour < 14
    if too_early and not hits:
        return check("seo_net_new_article", True,
                     f">=1 article on carrier day {target}",
                     f"carrier runs 11:00 PT; not due yet ({now:%H:%M} PT)")
    return check("seo_net_new_article", bool(hits),
                 f">=1 article published on carrier day {target}",
                 f"{len(hits)} published" + (f" ({hits[0]})" if hits else ""))


def seo_buffer() -> dict:
    proc = subprocess.run(
        ["python3", str(BUSINESS / "systems/seo-blog-machine/scripts/article_buffer_cli.py"), "status"],
        capture_output=True, text=True, cwd=BUSINESS)
    if proc.returncode != 0:
        return check("seo_article_buffer", False, ">=3 pre-validated articles ready",
                     f"BLOCKED_STATUS_RC rc={proc.returncode}")
    try:
        out = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return check("seo_article_buffer", False, ">=3 pre-validated articles ready",
                     "BLOCKED_STATUS_UNREADABLE")
    pool = out.get("pool") or {}
    ready, minimum = pool.get("ready"), pool.get("minimum")
    if not isinstance(ready, int) or not isinstance(minimum, int):
        return check("seo_article_buffer", False, ">=3 pre-validated articles ready",
                     "BLOCKED_POOL_UNREADABLE")
    dispatch_status = "not_needed"
    if ready < minimum:
        if str(BUSINESS / "scripts") not in sys.path:
            sys.path.insert(0, str(BUSINESS / "scripts"))
        import glp_article_carrier_selfheal as selfheal  # noqa: PLC0415

        dispatch = selfheal.check_supply(pool, emit=False)
        dispatch_status = str((dispatch.get("refill_dispatch") or {}).get("status") or dispatch["status"])
    raw_ready = out.get("raw_ready")
    invalid = len(out.get("invalid_entries") or [])
    return check("seo_article_buffer", ready >= minimum,
                 f">={minimum} pre-validated articles ready",
                 f"{ready} validated ready; raw_ready={raw_ready}; invalid={invalid}; refill={dispatch_status}")


def seo_topic_supply() -> dict:
    p = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260730/competitor-gap-feed-current.json"
    if not p.is_file():
        return check("seo_topic_supply", False, "supply feed exists with >=6 topics", "feed missing")
    d = read_json(p)
    gaps = int((d.get("summary") or {}).get("gaps") or 0)
    age_h = (dt.datetime.now(dt.timezone.utc)
             - dt.datetime.fromisoformat(str(d.get("created_at")).replace("Z", "+00:00"))).total_seconds() / 3600
    return check("seo_topic_supply", gaps >= 6 and age_h < 36,
                 ">=6 eligible topics, feed minted <36h ago",
                 f"{gaps} topics, minted {age_h:.0f}h ago")


def seo_update_carrier() -> dict:
    """Require a hash-valid same-due-day receipt proving at least one publish."""
    root = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-carrier"
    now = now_pt()
    target = now.date()
    if target.weekday() == 6 or (target.weekday() <= 5 and now.time() < dt.time(13, 15)):
        target -= dt.timedelta(days=1)
    while target.weekday() == 6:
        target -= dt.timedelta(days=1)
    receipts = sorted(
        root.glob(f"{target.isoformat()}/day-*/r2-day-receipt.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    ) if root.is_dir() else []
    expected = f">=1 R2 page published for due day {target.isoformat()}"
    if not receipts:
        return check("seo_update_carrier", False, expected, "day receipt missing")
    receipt = read_json(receipts[0])
    supplied_hash = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    actual_hash = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    schema_ok = receipt.get("schema") == "glp-seo-r2-day-receipt-v1"
    date_ok = receipt.get("pt_date") == target.isoformat()
    hash_ok = bool(supplied_hash) and supplied_hash == actual_hash
    published = int(receipt.get("published") or 0)
    completed = int(receipt.get("completed") or 0)
    required = int(receipt.get("required") or 0)
    deficit = int(receipt.get("deficit") or 0)
    status = str(receipt.get("status") or "UNKNOWN")
    ok = schema_ok and date_ok and hash_ok and published >= 1
    actual = (
        f"status={status}, published={published}, completed={completed}/{required}, "
        f"deficit={deficit}, schema_ok={schema_ok}, date_ok={date_ok}, hash_ok={hash_ok}"
    )
    return check("seo_update_carrier", ok, expected, actual)


def affiliate_harvest() -> dict:
    receipts = sorted(glob.glob(str(BUSINESS /
        "reports/glp-clinic-scout/affiliate-machine/site-evidence-harvest/daily-*/site-evidence-harvest.json")))
    if not receipts:
        return check("affiliate_evidence_harvest", False, "harvest receipts exist", "none found")
    d = read_json(Path(receipts[-1]))
    uncached = int(d.get("rescuable_uncached_before_cap") or 0)
    attempted = int(d.get("targets_selected") or 0)
    stalled = uncached > 0 and attempted == 0
    return check("affiliate_evidence_harvest", not stalled,
                 "backlog draining (fetches attempted while uncached advertisers remain)",
                 f"{uncached} uncached, {attempted} attempted last run")


def affiliate_application_queue() -> dict:
    config = read_json(BUSINESS / "config/affiliate-sites/glp-clinic-scout.json")
    jobs = read_json(HERMES / "cron/jobs.json").get("jobs") or []
    retired_ids = {"2dd6ae1a4db9", "52e2014a9579", "5a2cba16e057"}
    retired = [row for row in jobs if str(row.get("id")) in retired_ids]
    queue_only = (config.get("auto_apply") or {}).get("trigger") == "operator_queue_only"
    daily_paths_off = len(retired) == len(retired_ids) and all(
        row.get("enabled") is False and row.get("state") == "paused" for row in retired)
    return check(
        "affiliate_application_queue", queue_only and daily_paths_off,
        "applications run only from explicit operator queue rows; empty queue is healthy",
        f"queue_only={queue_only}, retired_daily_paths={sum(not row.get('enabled') for row in retired)}/{len(retired_ids)}",
    )


# Statuses that mean "we asked and the provider will not issue a link" — an
# honest, evidenced hold, not a machine that forgot. Verified 2026-08-16 against
# provider readbacks: Katalys 1501 pending with Destination disabled, 1618/1619
# not related to this publisher account, HealthTrader 347 is marketplace account
# access rather than a program. Flagging these forever would train the operator
# to ignore this sensor, which is the exact failure it exists to prevent.
HONEST_HOLD_STATUSES = frozenset({
    "approved_marketplace_access",              # account access, not a program
    "approved_superseded_by_existing_placement",# legal duplicate; existing placement wins
    "blocked_provider_relationship_not_active", # provider issues no link
    "duplicate_do_not_use",                     # suppressed duplicate listing
})


def affiliate_approved_but_unlinked() -> dict:
    """An approved, signed-up GLP program with no tracking link is invisible.

    Five approved Katalys providers (CoreAge Rx, Pallas Health, Bodybuilding.com
    GLP-1, SkinnyRx, RxSpan MD) sat `approved_active_not_wired` with an EMPTY
    tracking link. Without a link the row never becomes wire-ready, the B2 worker
    never queues it, and it can never go live — and nothing in the fleet noticed.
    The operator found it by asking where his approvals went (2026-08-16).
    """
    import sys
    sys.path.insert(0, str(BUSINESS / "scripts"))
    import glp_affiliate_wire_backlog_worker as worker
    from affiliate_sheet_schema_guard import REQUIRED_MARKETPLACE_PARENT_ROWS
    sheet = read_json(worker.latest_complete_backrun() / "sheet-full-row-export.json")
    headers = sheet["headers"]
    protected_marketplace_rows = set(REQUIRED_MARKETPLACE_PARENT_ROWS.values())
    stranded = []
    for row_number, values in sheet["rows"].items():
        # Marketplace parent records prove account access. They are not provider
        # offers and correctly have no provider tracking link of their own.
        if int(row_number) in protected_marketplace_rows:
            continue
        row = dict(zip(headers, values))
        # Network rails are account-access records that contain child offers;
        # the rail itself is not a merchant program and must not own a link.
        network_rail = str(row.get("Category") or "").strip().lower() == "network rail"
        approved = str(row.get("Status") or "").startswith("approved")
        glp = "glp1" in str(row.get("Vertical") or "").lower()
        signed = str(row.get("Signup Complete?") or "").upper() == "TRUE"
        live = str(row.get("Wired Live?") or "").upper() == "TRUE"
        link = str(row.get("Tracking Link / Deep Link") or "").strip()
        # A row can be approved on the SHEET while the provider has issued no
        # link at all — provider-pending, no active publisher relationship, or a
        # marketplace account-access record rather than a program. Those are
        # honest holds, verified 2026-08-16 (Katalys 333/463/464 provider-pending
        # or no relationship; HealthTrader 347 is account access, API 500).
        # Flagging them forever would train the operator to ignore this sensor,
        # which is the failure mode the whole sensor exists to prevent.
        held = str(row.get("Status") or "").strip() in HONEST_HOLD_STATUSES
        if approved and glp and signed and not live and not link and not held and not network_rail:
            stranded.append(str(row.get("Program / Network") or "")[:28])
    return check("affiliate_approved_but_unlinked", not stranded,
                 "no approved+signed-up GLP program left without a tracking link",
                 (f"{len(stranded)} stranded: " + ", ".join(stranded[:4])) if stranded
                 else "none stranded")


def affiliate_deploy() -> dict:
    d = read_json(BUSINESS / "reports/glp-clinic-scout/affiliate-machine/deploy-completeness-latest.json")
    age_hours = None
    try:
        captured = dt.datetime.fromisoformat(str(d.get("captured_at_utc", "")).replace("Z", "+00:00"))
        if captured.tzinfo is not None:
            age_hours = (dt.datetime.now(dt.timezone.utc) - captured).total_seconds() / 3600
    except (ValueError, TypeError):
        pass
    fresh = age_hours is not None and 0 <= age_hours <= 36
    return check("affiliate_site_deploy", fresh and d.get("status") == "PASS" and d.get("failure_count") == 0,
                 "deploy-completeness sensor PASS, 0 failures, receipt age <=36h",
                 f"{d.get('status')} with {d.get('failure_count')} failure(s), {d.get('live_programs')} live; receipt_age_hours={age_hours if age_hours is not None else 'UNKNOWN'}")


def inbox_monitor() -> dict:
    db = sqlite3.connect(f"file:{HERMES / 'state/email-monitor.sqlite3'}?mode=ro", uri=True)
    rows = list(db.execute(
        "SELECT mailbox_id, last_read_success_at, unhealthy_reason FROM provider_cursors "
        "WHERE last_read_success_at IS NOT NULL"))
    db.close()
    if not rows:
        return check("affiliate_inbox", False, "mailboxes being read", "no cursors")
    now = dt.datetime.now(dt.timezone.utc)
    stale = []
    for mid, read_at, bad in rows:
        try:
            age = (now - dt.datetime.fromisoformat(str(read_at).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = 1e9
        if age > 7200 or bad:
            stale.append(mid)
    return check("affiliate_inbox", not stale,
                 "every active mailbox read within 2h, none unhealthy",
                 f"{len(rows)} active, stale/unhealthy: {', '.join(stale) or 'none'}")


# RETIRED 2026-09-09 — Boring AI is a retired business (operator ruling: retire all
# Boring AI / Allocant / TLCC / avatar jobs; its Hermes cron rows were paused the same
# day, see reports/ops/orchestration-restore-20260907/config/CHANGES.md 20260909T010818Z
# and 20260909T010944Z). The boring_finder / boring_promotion checks asked whether a
# discovery lane that no longer runs had produced, so they reported "not producing"
# forever and diluted the GLP signal this sensor exists to carry. This sensor is now
# GLP-only. Preimage: config/backups/home-ubuntu-business-scripts-
# business_output_health_sensor.py-20260909T012244Z.

def seo_weekly_refresh_yield() -> dict:
    from ops.glp.seo_weekly_health import weekly_refresh_yield
    return weekly_refresh_yield()


def seo_r2_authority_fresh() -> dict:
    from ops.glp.seo_weekly_health import stale_authority
    return stale_authority()


CHECKS = (seo_net_new, seo_buffer, seo_topic_supply, seo_update_carrier,
          seo_weekly_refresh_yield, seo_r2_authority_fresh,
          affiliate_harvest, affiliate_application_queue, affiliate_approved_but_unlinked,
          affiliate_deploy, inbox_monitor)


def main() -> int:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(check(fn.__name__, False, "check executes", f"check crashed: {exc}"))
    bad = [r for r in results if not r["ok"]]
    receipt = {
        "schema": "business-output-health-v1",
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "healthy": len(results) - len(bad),
        "unhealthy": len(bad),
        "results": results,
    }
    out = BUSINESS / "reports/ops/output-health-latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if bad:
        lines = [f"🔴 **Output Health** — {len(bad)} machine(s) not producing",
                 ""]
        for r in bad:
            lines.append(f"**{r['machine']}** — expected {r['expected']}; actual: {r['actual']}")
        lines.append(f"→ every red line above is a machine that ran but did not produce; fix until it produces")
        lines.append(f"-# {receipt['healthy']}/{len(results)} healthy · receipt: {out}")
        print("\n".join(lines))
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
