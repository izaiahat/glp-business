from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path("/home/ubuntu/business")
HISTORICAL_CANDIDATES = ROOT / "reports/glp-clinic-scout/affiliate-machine/cron-domain-repair-20260811/candidates"
LIVE_WRAPPERS = Path("/home/ubuntu/.hermes/scripts")
JOBS = Path("/home/ubuntu/.hermes/cron/jobs.json")
HERMES_SCHEDULER = Path("/home/ubuntu/.hermes/hermes-agent/cron/scheduler.py")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(text: str, key: str) -> str:
    match = re.search(rf"{re.escape(key)}='([0-9a-f]{{64}})'", text)
    assert match, key
    return match.group(1)


def test_historical_candidate_wrappers_remain_shell_valid():
    for path in sorted(HISTORICAL_CANDIDATES.glob("*.sh")):
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_live_scoped_wrapper_pins_match_current_payloads_and_registry():
    """Registration digests stay current; internal payload pins are retired.

    On 2026-08-20 the cron runtime moved internal workers and Sheet
    reconciliation to current-on-disk execution. Registration digests became
    telemetry for those lanes, while provider-effecting jobs remained on the
    scheduler's selective refusal list. The retired daily auto-apply job is a
    special case: its registration may stay stale while paused so an accidental
    resume refuses before execution. Its no-apply retirement contract is tested
    separately below.
    """
    jobs = {row["id"]: row for row in json.loads(JOBS.read_text())["jobs"]}

    approval_path = LIVE_WRAPPERS / "glp_affiliate_approval_reconciler.sh"
    approval = approval_path.read_text()
    assert jobs["b0c800751b88"]["script_sha256"] == sha(approval_path)
    assert "scripts/affiliate_application_invitation_reconciler.py" in approval
    assert "scripts/affiliate_approval_auto_reconciler.py" in approval
    assert "expected_reconciler_sha256" not in approval
    assert "expected_invitation_sha256" not in approval

    auto_path = LIVE_WRAPPERS / "affiliate_auto_apply_daily.sh"
    auto = auto_path.read_text()
    assert jobs["2dd6ae1a4db9"]["enabled"] is False
    assert jobs["2dd6ae1a4db9"]["state"] == "paused"
    assert pin(auto, "expected_machine_sha256") == sha(ROOT / "scripts/glp_application_machine.py")
    assert "scripts/affiliate_auto_apply.py" not in auto
    assert "scripts/affiliate_auto_apply_card.py" not in auto

    daily_path = LIVE_WRAPPERS / "glp_affiliate_daily_report_cron.sh"
    daily = daily_path.read_text()
    assert jobs["ef70059fa85b"]["script_sha256"] == sha(daily_path)
    assert "scripts/glp_affiliate_daily_decisions_report.py" in daily
    assert "expected_report_sha256" not in daily


def test_retired_auto_apply_wrapper_has_no_apply_stage_and_is_scheduler_paused():
    """The 2026-08-24 retirement removed the stage, not merely its budget.

    Applications now drain explicit operator queue rows every 15 minutes through
    glp/applications. Keeping a timeout assertion for the retired 25/day engine
    would falsely require the old autonomous applier to remain executable.
    """
    text = (LIVE_WRAPPERS / "affiliate_auto_apply_daily.sh").read_text()
    job = next(row for row in json.loads(JOBS.read_text())["jobs"]
               if row["id"] == "2dd6ae1a4db9")
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert "operator decision 2026-08-24" in job["paused_reason"]
    for retired in ("timeout ", "--deadline-seconds", "affiliate_auto_apply.py",
                    "affiliate_auto_apply_card.py", "daily_cap"):
        assert retired not in text
    assert "this daily path is retired" in text
    assert "exit 0" in text


def test_retired_auto_apply_failure_card_has_no_phantom_stage_budgets():
    """The retired wrapper may report pin refusal, never imaginary run budgets.

    This replaces the pre-2026-08-24 assertion that required the failure card to
    quote a deadline from an application stage the operator removed.
    """
    text = (LIVE_WRAPPERS / "affiliate_auto_apply_daily.sh").read_text()
    assert "queue-drain payload pin mismatch; retired wrapper refused" in text
    assert "exit 2" in text
    for phantom in ("inner deadline:", "descendant kill bound:",
                    "scheduler timeout:", "application-deadline"):
        assert phantom not in text


def test_cron_pin_reconcile_and_verify_pins_match_the_machinery_it_runs():
    """Assert the 2026-08-20 telemetry stub and selective scheduler enforcement.

    The old self-pinned reconciler was intentionally replaced: internal jobs run
    descriptor-bound current bytes, drift is informational, and provider-effect
    jobs retain scheduler-owned hash refusal. Restoring the old embedded
    RECONCILER/MAPPING/SENSOR pins would reverse that policy change.
    """
    shell_path = LIVE_WRAPPERS / "cron_pin_reconcile_and_verify.sh"
    text = shell_path.read_text()
    assert "Legacy compatibility entrypoint. Registration digests are telemetry only." in text
    assert "readonly SENSOR=/home/ubuntu/.hermes/scripts/cron_script_pin_drift_sensor.py" in text
    assert 'exec /usr/bin/python3 "$SENSOR"' in text
    assert "cron_pin_reconciler.py" not in text
    assert "cron_pin_test_map.json" not in text
    assert not re.search(r"[0-9a-f]{64}", text)

    sensor = (LIVE_WRAPPERS / "cron_script_pin_drift_sensor.py").read_text()
    assert '"status": "PASS" if not drift else "INFORMATIONAL_DRIFT"' in sensor
    assert "no action required; internal jobs execute their current on-disk scripts" in sensor
    assert "return 0" in sensor

    scheduler = HERMES_SCHEDULER.read_text()
    enforced = re.search(r"_SCRIPT_HASH_ENFORCED_JOBS\s*=\s*\{(?P<body>.*?)\n\}",
                         scheduler, re.S)
    assert enforced, "scheduler selective hash-refusal registry missing"
    body = enforced.group("body")
    assert '"2dd6ae1a4db9": "affiliate_auto_apply_daily.sh"' in body
    assert "b0c800751b88" not in body  # internal Sheet reconciliation
    assert "ef70059fa85b" not in body  # internal report
    assert "if enforce_script_sha256:" in scheduler
    assert 'raise RuntimeError("cron_script_hash_mismatch")' in scheduler
    assert "running the current on-disk bytes" in scheduler

    jobs = json.loads(JOBS.read_text())["jobs"]
    assert all(Path(str(row.get("script") or "")).name != "cron_pin_reconciler.py"
               for row in jobs)
    telemetry_job = next(row for row in jobs if row["id"] == "ee29ebacbb7e")
    assert telemetry_job["enabled"] is False
    assert telemetry_job["script"] == shell_path.name


def test_candidates_are_local_only_and_never_modify_protected_assets():
    protected = {
        "affiliate_sheet_schema_guard.py", "check_affiliate_sheet_writer_authority.py",
        "jobs.json", "STATE.md", "google_token.json",
    }
    for path in HISTORICAL_CANDIDATES.glob("*.sh"):
        text = path.read_text()
        assert "values().update" not in text
        assert "messages().send" not in text
        assert not any(f">{name}" in text or f"> {name}" in text for name in protected)
        assert "AFFILIATE_CRON_VERIFY_ONLY" in text
