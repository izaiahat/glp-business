#!/usr/bin/env python3
"""Immediate operator alert when an expected SEO publication lane is failing.

Operator ruling 2026-08-14: "A publishing failure should notify me immediately.
That should never happen." This fires as soon as the day's carrier run has
terminally failed without satisfying the scheduled NEW_ROUTE and R2_REFRESH
publication lanes for this PT day — hours before the 23:17 close watchdog. One
urgent page per PT day, with an explicit recovery notice if the lanes recover.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ubuntu/business/ops/operator-console")
import red_alert_hook  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_outcome_cards as outcome_cards  # noqa: E402
sys.path.append('/home/ubuntu/business/scripts')
from article_carrier_gate_health import accepted_failure_health  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
CARRIER_JOB = "b8948c536d31"
EXEC_DB = Path("/home/ubuntu/.hermes/cron/executions.db")
LEDGER = Path("/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/published-route-ledger.json")
STATE = Path("/home/ubuntu/.hermes/state/glp-seo-publish-failure-alert.json")


def pt_today() -> str:
    return dt.datetime.now(PT).date().isoformat()


def new_routes_today(day: str) -> list[str]:
    if not LEDGER.is_file():
        return []
    routes = json.loads(LEDGER.read_text()).get("routes", {})
    published = []
    start = dt.datetime.fromisoformat(day).replace(tzinfo=PT)
    end = start + dt.timedelta(days=1)
    for route, row in routes.items():
        seen = str(row.get("seen_at_utc") or "")
        if not seen:
            continue
        try:
            when = dt.datetime.fromisoformat(seen.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start <= when.astimezone(PT) < end and row.get("announced"):
            published.append(route)
    return sorted(published)


def publication_summary(day: str) -> dict[str, object]:
    """Return NEW_ROUTE and signed R2_REFRESH truth as separate lanes."""
    day_date = dt.date.fromisoformat(day)
    routes = new_routes_today(day)
    refresh = outcome_cards.r2_refreshes_today(day_date)
    new_scheduled = day_date.weekday() in (0, 2, 4)  # Mon/Wed/Fri
    receipt = refresh.get("receipt")
    return {
        "schema": "glp-seo-publication-summary-v1",
        "pt_date": day,
        "new_route": {
            "kind": "NEW_ROUTE",
            "scheduled": new_scheduled,
            "cadence": "M/W/F",
            "published_count": len(routes),
            "routes": routes,
            "source": "published-route-ledger.json",
            "accepted_failures": accepted_failure_health(day),
        },
        "r2_refresh": {
            "kind": "R2_REFRESH",
            "scheduled": bool(refresh["scheduled"]),
            "cadence": "Mon-Sat",
            "ok": bool(refresh["ok"]),
            "status": str(refresh["status"]),
            # Missing signed evidence stays unknown, never a fabricated zero.
            "required": None if refresh["required"] is None else int(refresh["required"]),
            "completed": None if refresh["completed"] is None else int(refresh["completed"]),
            "published_count": None if refresh["published"] is None else int(refresh["published"]),
            "deficit": refresh["deficit"],
            "receipt_path": receipt,
            "receipt_hash_valid": bool(
                receipt and refresh["status"] != "INVALID_DAY_RECEIPT"
            ),
        },
    }


def format_summary(summary: dict[str, object]) -> str:
    new = summary["new_route"]
    refresh = summary["r2_refresh"]
    assert isinstance(new, dict) and isinstance(refresh, dict)
    gate = new.get("accepted_failures") or {}
    new_status = (
        "NOT_SCHEDULED" if not new["scheduled"]
        else "PASS" if int(new["published_count"]) > 0
        else "FAIL"
    )
    return (
        f"NEW_ROUTE {new_status}: published {new['published_count']} "
        f"(cadence {new['cadence']}); "
        f"suite accepted {gate.get('accepted_inherited_count', 'UNKNOWN')} / new {gate.get('new_failure_count', 'UNKNOWN')}; "
        f"R2_REFRESH {refresh['status']}: published {refresh['published_count']}, "
        f"completed {refresh['completed']}/{refresh['required']}, "
        f"deficit {refresh['deficit']}"
    )


def carrier_terminal(day: str) -> str | None:
    """The day's latest carrier execution status, or None if it has not run."""
    db = sqlite3.connect(EXEC_DB)
    table = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")][0]
    row = db.execute(
        f"SELECT status, started_at FROM {table} WHERE job_id=? "
        f"ORDER BY rowid DESC LIMIT 1", (CARRIER_JOB,)).fetchone()
    if not row:
        return None
    started = dt.datetime.fromisoformat(row[1]).astimezone(PT)
    if started.date().isoformat() != day:
        return None
    return str(row[0])


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {}


def main() -> int:
    day = pt_today()
    state = load_state()
    summary = publication_summary(day)
    new = summary["new_route"]
    refresh = summary["r2_refresh"]
    assert isinstance(new, dict) and isinstance(refresh, dict)
    terminal = carrier_terminal(day)
    terminal_run = terminal in ("completed", "failed", "blocked")
    failures = []
    if terminal_run and new["scheduled"] and int(new["published_count"]) == 0:
        failures.append("NEW_ROUTE")
    if terminal_run and refresh["scheduled"] and not refresh["ok"]:
        failures.append("R2_REFRESH")
    all_scheduled_lanes_healthy = (
        (not new["scheduled"] or int(new["published_count"]) > 0)
        and (not refresh["scheduled"] or bool(refresh["ok"]))
    )

    if all_scheduled_lanes_healthy:
        if state.get("alerted_day") == day and not state.get("recovered"):
            red_alert_hook.raise_alert({
                "severity": "RED", "source": "SEO Publish",
                "impact": "recovered — scheduled SEO publication lanes are healthy",
                "what": f"{format_summary(summary)}. The earlier failure self-resolved.",
                "action": "No action needed.",
                "where": "glpclinicscout.com · NEW_ROUTE ledger · R2 day receipt",
                "evidence_codes": [], "url": None, "url_title": None,
                "dedup_key": f"SEO Publish:recovery:{day}",
                "emergency": False, "recovery": True,
            }, live=True)
            state["recovered"] = True
            STATE.write_text(json.dumps(state))
        print(f"ok: {format_summary(summary)} on {day}")
        return 0

    if failures:
        if state.get("alerted_day") != day:
            red_alert_hook.raise_alert({
                "severity": "RED", "source": "SEO Publish",
                "impact": "scheduled SEO publication lane failed: " + ", ".join(failures),
                "what": (f"The {day} carrier run ended '{terminal}'. "
                         f"{format_summary(summary)}."),
                "action": ("Bridge/autopilot are on it; the run receipt names the exact "
                           "gate. Restore only the failed scheduled lane(s)."),
                "where": "/home/ubuntu/.hermes/cron/output/b8948c536d31/ · #ops-alerts",
                "evidence_codes": [], "url": None, "url_title": None,
                "dedup_key": f"SEO Publish:publication-lanes:{day}",
                "emergency": False, "recovery": False,
            }, live=True)
            STATE.write_text(json.dumps({"alerted_day": day, "recovered": False}))
            print(
                f"ALERTED: carrier={terminal}, failures={'+'.join(failures)}, "
                f"{format_summary(summary)} on {day}"
            )
        else:
            print(f"already alerted for {day}")
        return 0

    print(f"waiting: carrier={terminal}, {format_summary(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
