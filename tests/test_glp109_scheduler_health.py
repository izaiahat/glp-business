"""GLP-109 scheduler health fault tests (isolated tmp fixtures; no host paths, no sends)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

UTC = dt.timezone.utc
# 2026-10-03 16:30Z == 09:30 PDT
NOW = dt.datetime(2026, 10, 3, 16, 30, tzinfo=UTC)
CLOCK = [NOW]


@pytest.fixture
def sensor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "glp109_output_health", ROOT / "scripts/business_output_health_sensor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    CLOCK[0] = NOW
    hermes, business = tmp_path / "hermes", tmp_path / "business"
    (hermes / "cron").mkdir(parents=True)
    monkeypatch.setattr(module, "HERMES", hermes)
    monkeypatch.setattr(module, "BUSINESS", business)
    monkeypatch.setattr(module, "EPISODE_STATE", business / "ops/state/episodes.json")

    class FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK[0].astimezone(tz) if tz else CLOCK[0].replace(tzinfo=None)

    monkeypatch.setattr(module.dt, "datetime", FrozenDatetime)
    return module


def write_jobs(sensor, *jobs):
    (sensor.HERMES / "cron/jobs.json").write_text(json.dumps({"jobs": list(jobs)}))


def deliver(sensor, text):
    """What Hermes saves for the registered output-health cron run."""
    root = sensor.HERMES / "cron/output" / sensor.OUTPUT_HEALTH_JOB_ID
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"run-{len(list(root.iterdir()))}.md"
    path.write_text(f"## Response\n\n{text}\n")
    stamp = NOW.timestamp() + 60
    os.utime(path, (stamp, stamp))


def job(**fields):
    base = {"id": "abcdef123456", "name": "GLP daily collector", "enabled": True,
            "last_status": "ok", "failure_streak": 0,
            "last_run_at": "2026-10-03T08:00:00-07:00", "next_run_at": "2026-10-04T08:00:00-07:00"}
    return {**base, **fields}


# --- acceptance: timezone-correct overdue predicate -------------------------

def test_overdue_distinguishes_clock_misread_from_genuinely_late(sensor):
    # Naive 09:00 is scheduler-local (PDT): 30m ago, inside grace. Read as UTC it
    # would be 7h30m late -- the old misread. Must not be called late.
    misread = job(next_run_at="2026-10-03T09:00:00", last_run_at="2026-10-02T09:00:00")
    assert sensor.overdue_state(misread, NOW)[0] == "clock_misread"
    # Aware 08:00 PDT with no run since: genuinely 90m late.
    late = job(next_run_at="2026-10-03T08:00:00-07:00", last_run_at="2026-10-02T08:00:00-07:00")
    assert sensor.overdue_state(late, NOW)[0] == "late"
    # Naive local time that is truly late stays late.
    assert sensor.overdue_state(job(next_run_at="2026-10-03T07:00:00",
                                    last_run_at="2026-10-02T07:00:00"), NOW)[0] == "late"
    # A last run stamped in the observer's future is a clock fault, not lateness.
    ahead = job(next_run_at="2026-10-03T08:00:00-07:00", last_run_at="2026-10-03T11:00:00-07:00")
    assert sensor.overdue_state(ahead, NOW)[0] == "clock_misread"
    assert sensor.overdue_state(job(), NOW)[0] == "on_time"


# --- acceptance: SQLite handle recovery after I/O errors ---------------------

class FakeConnection:
    def __init__(self, fail):
        self.fail, self.closed = fail, False

    def close(self):
        self.closed = True

    def execute(self, _sql):
        if self.closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        return iter([("box-1", NOW.isoformat(), None)])


def test_sqlite_io_error_closes_stale_handle_and_reads_on_fresh_one(sensor, monkeypatch):
    handles = []

    def connect(*_args, **_kwargs):
        handles.append(FakeConnection(fail=not handles))
        return handles[-1]

    monkeypatch.setattr(sensor.sqlite3, "connect", connect)
    result = sensor.inbox_monitor()
    assert result["ok"] is True, result
    assert len(handles) == 2 and handles[0] is not handles[1]
    assert all(h.closed for h in handles)


def test_sqlite_persistent_io_error_is_reported_not_crashed(sensor, monkeypatch):
    handles = []

    def connect(*_args, **_kwargs):
        handles.append(FakeConnection(fail=True))
        return handles[-1]

    monkeypatch.setattr(sensor.sqlite3, "connect", connect)
    result = sensor.inbox_monitor()
    assert result["ok"] is False and "disk I/O error" in result["actual"]
    assert len(handles) == 2 and all(h.closed for h in handles)


# --- acceptance: 429/quota re-arm --------------------------------------------

def test_quota_failure_is_healthy_only_while_rearmed(sensor):
    rearmed = job(last_status="error", failure_streak=1, last_error="HTTP 429 quota exceeded")
    write_jobs(sensor, rearmed)
    assert sensor.glp_scheduler_jobs()["ok"] is True

    stuck = job(last_status="error", failure_streak=1, last_error="HTTP 429 quota exceeded",
                next_run_at=None)
    write_jobs(sensor, stuck)
    result = sensor.glp_scheduler_jobs()
    assert result["ok"] is False and result["jobs"][0]["problem"] == "quota_not_rearmed"


# --- acceptance: deduplicated alert after 2+ consecutive errors --------------

def test_two_consecutive_errors_alert_once_per_episode_and_rearm_after_recovery(sensor):
    write_jobs(sensor, job(last_status="error", failure_streak=1))
    assert sensor.glp_scheduler_jobs()["ok"] is True          # one error: no escalation

    write_jobs(sensor, job(last_status="error", failure_streak=2))
    first = sensor.glp_scheduler_jobs()
    assert first["ok"] is False and first["alert"] is True
    assert "episode abcdef123456:repeated_errors" in first["actual"]

    # Undelivered (e.g. the SEO updater's child run): still alertable next tick.
    assert sensor.glp_scheduler_jobs()["alert"] is True
    deliver(sensor, first["actual"])
    write_jobs(sensor, job(last_status="error", failure_streak=3,
                           last_run_at="2026-10-03T09:15:00-07:00"))
    again = sensor.glp_scheduler_jobs()
    assert again["ok"] is False and again["alert"] is False   # same episode: deduped

    write_jobs(sensor, job())                                  # recovered: episode cleared
    assert sensor.glp_scheduler_jobs()["ok"] is True
    CLOCK[0] = NOW + dt.timedelta(hours=6)                     # later, a new failure episode
    write_jobs(sensor, job(last_status="error", failure_streak=2))
    assert sensor.glp_scheduler_jobs()["alert"] is True        # re-armed for the next episode


def test_main_prints_new_episode_and_stays_silent_when_deduped(sensor, monkeypatch, capsys):
    monkeypatch.setattr(sensor, "CHECKS", (sensor.glp_scheduler_jobs,))
    write_jobs(sensor, job(last_status="error", failure_streak=2))
    assert sensor.main() == 1
    printed = capsys.readouterr().out
    assert "glp_scheduler_jobs" in printed and "episode abcdef123456:repeated_errors" in printed
    deliver(sensor, printed)
    assert sensor.main() == 0 and capsys.readouterr().out == ""
    receipt = json.loads((sensor.BUSINESS / "reports/ops/output-health-latest.json").read_text())
    assert receipt["unhealthy"] == 1                            # still red in the table


# --- acceptance: health table links to real output ---------------------------

def test_health_table_links_newest_real_output_not_last_status(sensor):
    root = sensor.HERMES / "cron/output/abcdef123456"
    root.mkdir(parents=True)
    old, new = root / "old.md", root / "new.md"
    old.write_text("old")
    new.write_text("new")
    os.utime(old, (NOW.timestamp() - 86400 * 2,) * 2)
    os.utime(new, (NOW.timestamp() - 3600,) * 2)
    write_jobs(sensor, job(last_run_at="2026-10-03T08:00:00-07:00"))
    row = sensor.glp_scheduler_jobs()["jobs"][0]
    assert row["output"] == str(new)
    assert row["output_after_last_run"] is True
    write_jobs(sensor, job(last_run_at="2026-10-03T09:00:00-07:00"))  # ran, no newer output
    row = sensor.glp_scheduler_jobs()["jobs"][0]
    assert row["last_status"] == "ok" and row["output_after_last_run"] is False


# --- regression: non-GLP and paused jobs stay out of the GLP table -----------

def test_paused_and_non_glp_jobs_are_not_judged(sensor):
    write_jobs(sensor,
               job(id="aaaaaaaaaaaa", name="Other lane", last_status="error", failure_streak=5),
               job(id="bbbbbbbbbbbb", enabled=False, state="paused", last_status="error",
                   failure_streak=5))
    result = sensor.glp_scheduler_jobs()
    assert result["ok"] is True and result["jobs"] == []
