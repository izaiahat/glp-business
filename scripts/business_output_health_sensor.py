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
other check only reads ledgers/receipts the machines already write; the GLP
scheduler check also keeps a first-seen file per open alert episode.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
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


def read_sqlite_ro(path: Path, sql: str) -> list:
    """Read with a fresh read-only handle; a handle that raised is closed, never reused.

    GLP-109: one `disk I/O error` left the old handle open and crashed the check, so
    the inbox row read "check crashed" until the next tick. Retry once on a NEW
    connection; a second failure is raised to the caller as real evidence.
    """
    for attempt in (1, 2):
        db = None
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            return list(db.execute(sql))
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
        finally:
            if db is not None:
                db.close()
    raise AssertionError("unreachable")


def inbox_monitor() -> dict:
    try:
        rows = read_sqlite_ro(
            HERMES / "state/email-monitor.sqlite3",
            "SELECT mailbox_id, last_read_success_at, unhealthy_reason FROM provider_cursors "
            "WHERE last_read_success_at IS NOT NULL")
    except sqlite3.OperationalError as exc:
        return check("affiliate_inbox", False, "mailboxes being read",
                     f"cursor DB unreadable after fresh-handle retry: {exc}")
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


# GLP-109 scheduler health. Hermes writes a naive cron timestamp in the host
# scheduler zone, not UTC; reading it as UTC made on-time PT jobs look ~7h late.
SCHEDULER_TZ = PT
OVERDUE_GRACE = dt.timedelta(minutes=45)   # scheduled_job_health_manager stale_due_grace_minutes default
CLOCK_SKEW = dt.timedelta(minutes=5)
QUOTA_RX = re.compile(r"HTTP 429|\b429\b|quota|rate.?limit|RESOURCE_EXHAUSTED|RateLimitError", re.I)
OUTPUT_HEALTH_JOB_ID = "eb6a4b2a47a3"      # registered business_output_health_cron.sh entry
EPISODE_STATE = BUSINESS / "ops/state/output-health-scheduler-episodes.json"


def scheduler_time(value) -> tuple[dt.datetime | None, bool]:
    """Parse a scheduler timestamp; returns (aware time, was_naive)."""
    if not isinstance(value, str) or not value.strip():
        return None, False
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SCHEDULER_TZ), True
    return parsed, False


def overdue_state(job: dict, now: dt.datetime) -> tuple[str, str]:
    """on_time | late | clock_misread | unscheduled.

    clock_misread means the timestamps disagree with the observer's clock (a
    naive value that only looks late when misread as UTC, or a last run stamped
    in the future); late means a consistent due time passed grace with no run.
    """
    raw = job.get("next_run_at")
    next_run, naive = scheduler_time(raw)
    if next_run is None:
        return "unscheduled", f"next_run_at={raw!r}"
    last_run, _ = scheduler_time(job.get("last_run_at"))
    if last_run is not None and last_run - now > CLOCK_SKEW:
        return "clock_misread", f"last_run_at {job.get('last_run_at')} is ahead of observer clock {now.isoformat()}"
    late = now - next_run
    if late > OVERDUE_GRACE:
        return "late", f"next_run_at {raw} is {int(late.total_seconds() // 60)}m past due"
    if naive and now - next_run.replace(tzinfo=dt.timezone.utc) > OVERDUE_GRACE:
        return "clock_misread", f"naive next_run_at {raw} looks late only when read as UTC"
    return "on_time", f"next_run_at {raw}"


def latest_job_output(job_id: str) -> tuple[Path | None, dt.datetime | None]:
    root = HERMES / "cron/output" / job_id
    files = sorted(root.glob("*.md"), key=lambda p: p.stat().st_mtime) if root.is_dir() else []
    if not files:
        return None, None
    return files[-1], dt.datetime.fromtimestamp(files[-1].stat().st_mtime, dt.timezone.utc)


def job_problem(job: dict, now: dt.datetime) -> tuple[str | None, str, str]:
    """Return (problem kind or None, overdue state, detail)."""
    status = str(job.get("last_status") or "").lower()
    streak = job.get("failure_streak", 0)
    streak = streak if type(streak) is int and streak >= 0 else 0
    quota = status == "error" and bool(QUOTA_RX.search(str(job.get("last_error") or "")))
    state, detail = overdue_state(job, now)
    # A 429/quota failure is transient only while the scheduler has re-armed the
    # job for a future run; a quota-failed job left without a live due time is dead.
    if quota and state in {"unscheduled", "late"}:
        return "quota_not_rearmed", state, f"429/quota error and not re-armed ({detail})"
    if status == "error" and streak >= 2:
        return ("quota_errors" if quota else "repeated_errors"), state, f"{streak} consecutive errors"
    if state in {"late", "clock_misread"}:
        return state, state, detail
    return None, state, detail


def episode_delivered(key: str, first_seen: dt.datetime) -> bool:
    """Proof of delivery is this sensor's own registered cron output, not a local flag.

    The SEO updater also runs this sensor as a child; a state-only "alerted" bit
    would let that undelivered run swallow the ops-alerts card.
    """
    root = HERMES / "cron/output" / OUTPUT_HEALTH_JOB_ID
    if not root.is_dir():
        return False
    marker = f"episode {key}"
    floor = first_seen.timestamp() - 1
    for path in root.glob("*.md"):
        try:
            if path.stat().st_mtime >= floor and marker in path.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def save_episodes(episodes: dict) -> None:
    EPISODE_STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".output-health-episodes-", dir=EPISODE_STATE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"schema": "output-health-scheduler-episodes-v1", "episodes": episodes},
                      out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(name, EPISODE_STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def glp_scheduler_jobs() -> dict:
    """GLP scheduler rows: overdue, quota re-arm, 2+ consecutive errors, real output link."""
    now = dt.datetime.now(dt.timezone.utc)
    data = read_json(HERMES / "cron/jobs.json")
    jobs = data.get("jobs") if isinstance(data, dict) else data
    previous = read_json(EPISODE_STATE).get("episodes", {}) if EPISODE_STATE.is_file() else {}
    table, problems, episodes = [], [], {}
    for job in jobs or []:
        if not isinstance(job, dict) or job.get("enabled") is False or job.get("state") == "paused":
            continue
        if not re.search(r"glp", str(job.get("name") or ""), re.I):
            continue
        jid = str(job.get("id") or "")
        kind, state, detail = job_problem(job, now)
        output, output_at = latest_job_output(jid)
        last_run, _ = scheduler_time(job.get("last_run_at"))
        table.append({
            "job_id": jid, "name": str(job.get("name") or ""),
            "last_status": job.get("last_status"), "failure_streak": job.get("failure_streak"),
            "next_run_at": job.get("next_run_at"), "overdue": state,
            "output": str(output) if output else None,
            "output_at_utc": output_at.isoformat() if output_at else None,
            "output_after_last_run": (output_at >= last_run) if output_at and last_run else None,
            "problem": kind,
        })
        if kind:
            key = f"{jid}:{kind}"
            first_seen = str((previous.get(key) or {}).get("first_seen_at_utc") or now.isoformat())
            episodes[key] = {"first_seen_at_utc": first_seen}
            new = not episode_delivered(key, dt.datetime.fromisoformat(first_seen))
            problems.append((key, new, f"{job.get('name')} {kind}: {detail}; output {output or 'none'}"))
    # Keys absent this run are dropped: a cleared episode re-arms its next alert.
    save_episodes(episodes)
    result = check("glp_scheduler_jobs", not problems,
                   "GLP jobs on time, re-armed after 429/quota, <2 consecutive errors",
                   "; ".join(f"{text} [episode {key}]" for key, _new, text in problems) or
                   f"{len(table)} GLP jobs healthy")
    result["alert"] = any(new for _key, new, _text in problems)
    result["jobs"] = table
    return result


CHECKS = (seo_net_new, seo_buffer, seo_topic_supply, seo_update_carrier,
          seo_weekly_refresh_yield, seo_r2_authority_fresh,
          affiliate_harvest, affiliate_application_queue, affiliate_approved_but_unlinked,
          affiliate_deploy, inbox_monitor, glp_scheduler_jobs)


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
    # Deduplicated rows stay red in the receipt; only an undelivered episode re-posts.
    bad = [r for r in bad if r.get("alert", True)]
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
