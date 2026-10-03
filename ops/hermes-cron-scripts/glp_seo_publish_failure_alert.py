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
import fcntl
import hashlib
import json
import re
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
HOST_RUNS = Path("/home/ubuntu/business/reports/ops/n8n-to-cron-20260924/runs")
HOST_ACTION = "article_carrier_consume"
HOST_DIGEST = hashlib.sha256(json.dumps({
    "action": "script", "name": HOST_ACTION, "args": {"mode": "consume"},
}, sort_keys=True).encode()).hexdigest()


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


def host_carrier_terminal(day: str) -> str | None:
    """Read only the registered consume branch under its existing writer lock.

    Unknown/pending/held receipts are not terminal failures or retry authority.
    Never scan glob/mtime for a convenient older successful or failed receipt.
    """
    try:
        with (HOST_RUNS / f"{HOST_ACTION}.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            state = json.loads((HOST_RUNS / f"{HOST_ACTION}-{HOST_DIGEST}.state.json").read_text())
            common_path = HOST_RUNS / f"{HOST_ACTION}.state.json"
            common = json.loads(common_path.read_text()) if common_path.exists() else {}
            if "pending" in state or "pending" in common:
                return None
            path = Path(state["last_receipt"])
            if (path.parent != HOST_RUNS or path.is_symlink()
                    or not re.fullmatch(r"article_carrier_consume-[0-9a-f]{32}\.json", path.name)):
                return None
            receipt = json.loads(path.read_text())
            slot = receipt["due_slot"]
            if (receipt["action"] != HOST_ACTION or receipt["payload_sha256"] != HOST_DIGEST
                    or receipt["receipt_path"] != str(path)
                    or receipt["invocation"] not in ("scheduled", "manual_registered")
                    or receipt.get("run_type", receipt["invocation"]) != receipt["invocation"]
                    or not isinstance(slot, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T11:00:America/Los_Angeles", slot)
                    or type(receipt["rc"]) is not int
                    or state["slots"][slot] != {"rc": receipt["rc"], "receipt_path": str(path)}):
                return None
            start = dt.datetime.fromisoformat(receipt["started_utc"])
            finish = dt.datetime.fromisoformat(receipt["finished_utc"])
            boundary = dt.datetime.fromisoformat(slot[:16]).replace(tzinfo=PT)
            if (start.tzinfo is None or finish.tzinfo is None or finish < start
                    or start.astimezone(PT).date().isoformat() != day
                    or boundary.weekday() not in (0, 2, 4) or start < boundary):
                return None
            bridge = receipt.get("bridge") or {}
            terminal = (bridge.get("status") in ("done", "error")
                        and isinstance(bridge.get("job_id"), str) and bool(bridge["job_id"])
                        and type(bridge.get("rc")) is int
                        and not bridge.get("timed_out") and not bridge.get("interrupted")
                        and (bridge.get("termination") or {}).get("classification") != "BRIDGE_INTERRUPTED")
            rejected = receipt.get("reason") in {
                f"bridge_rejected_http_{code}" for code in (400, 401, 403, 404, 413, 429)}
            if receipt["status"] == "FAIL" and receipt["rc"] != 0 and (terminal or rejected):
                return "failed"
            if (receipt["status"] == "PASS" and receipt["rc"] == 0 and terminal
                    and bridge["status"] == "done" and bridge["rc"] == 0):
                return "completed"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


def carrier_terminal(day: str) -> str | None:
    """Prefer host custody; native fallback only before host-state migration."""
    if ((HOST_RUNS / f"{HOST_ACTION}-{HOST_DIGEST}.state.json").exists()
            or (HOST_RUNS / f"{HOST_ACTION}.state.json").exists()):
        return host_carrier_terminal(day)
    # Retain genuine legacy consumers, without creating a missing database.
    try:
        with sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT status, started_at FROM executions WHERE job_id=? "
                "ORDER BY rowid DESC LIMIT 1", (CARRIER_JOB,)).fetchone()
        if not row:
            return None
        started = dt.datetime.fromisoformat(row[1])
        if started.tzinfo is None or started.astimezone(PT).date().isoformat() != day:
            return None
        return str(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None


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
                "where": f"{HOST_RUNS}/ · native fallback {CARRIER_JOB} · #ops-alerts",
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
