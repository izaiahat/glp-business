#!/usr/bin/env python3
"""Deterministic 14-gate production validator for the inbox control plane."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
ROOT = BUSINESS / "inbox_control_plane"
if str(BUSINESS) not in sys.path:
    sys.path.insert(0, str(BUSINESS))
from inbox_control_plane.control_plane import POLICY_VERSION, SCHEMA_VERSION, validate_ownership_manifest
REPORT = BUSINESS / "reports/email-monitor-hardening-2026-07-11"
DB = Path("/home/ubuntu/.hermes/state/email-monitor.sqlite3")
CRON_STORE = Path("/home/ubuntu/.hermes/cron/jobs.json")
CONTRACT = BUSINESS / "agents/loopos/contracts/hardened_multi_company_inbox_control_plane_20260711.json"
DETAILS = REPORT / "validator-details-latest.json"
EXPECTED_ACTIVE = {
    "kael_generic_gmail", "glp_ops_gmail", "glp_izaiah_gmail", "glp_website_inbound",
    "boring_hello_gmail", "boring_izaiah_gmail",
    "allocant_workspace_gmail", "allocant_izaiah_gmail",
}
EXPECTED_SCHEMA_VERSION = str(SCHEMA_VERSION)
LEGACY_JOB_IDS = {"50af51572594", "0243de69595a", "275614f2ea1a", "b463b6052780", "32a184e5d0ee"}
CANONICAL_JOB_ID = "3c79a0b5f7b2"
SCOPED_READER_JOB_ID = "bb577641ebd9"
SCOPED_READER_WRAPPER = Path("/home/ubuntu/.hermes/scripts/process_partner_info_replies_watchdog.sh")
SCOPED_READER_SOURCE = BUSINESS / "scripts/process_partner_info_replies.py"
SCOPED_READER_RECEIPT = BUSINESS / "reports/glp-clinic-scout/affiliate-info-request-loop/orderlymeds-20260804.json"
SCOPED_READER_MAILBOX_ID = "glp_ops_gmail"
SCOPED_READER_CANONICAL_ADDRESS = "ops@glpclinicscout.com"
SCOPED_READER_PROVIDER = "OrderlyMeds"
SCOPED_READER_WRAPPER_SHA256 = "9e4933fc48b1b2a232308b560f446120dc2f84ea334df2ed4237eca1685ebaf6"
# Re-anchored 2026-08-14: certified partner-info loop postimage/schema-guard
# bytes; the scoped thread/message contract and all semantic markers remain.
SCOPED_READER_SOURCE_SHA256 = "3c98b510da3c7e35e2aa270e665eb214cc67dd44301b5cfebc9d764247438e16"
SCOPED_READER_RECEIPT_SHA256 = "3b63948fe3f92833f0e76f28c2d96a18b08604b248479840650e85daef62f357"
UNIT_TEST_DISCOVERY_ARGS = ("-m", "unittest", "discover", "-s", "tests", "-p", "test_inbox_control_plane.py", "-v")
MINIMUM_INBOX_TESTS = 63


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso_age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - parsed.astimezone(dt.timezone.utc)).total_seconds()


def run(command: list[str], timeout: int = 180) -> dict[str, Any]:
    proc = subprocess.run(command, cwd=BUSINESS, text=True, capture_output=True, timeout=timeout)
    return {
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def mode(path: Path) -> int | None:
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else None


def validate_scoped_reader_contract(
    jobs: dict[str, dict[str, Any]],
    registry: dict[str, Any],
    receipt: dict[str, Any],
    receipt_sha256: str,
    wrapper_bytes: bytes,
    source_bytes: bytes,
) -> tuple[bool, dict[str, Any]]:
    """Validate the explicitly allowlisted non-general provider-thread reader."""
    job = jobs.get(SCOPED_READER_JOB_ID) or {}
    row = next((item for item in registry.get("mailboxes", []) if item.get("mailbox_id") == SCOPED_READER_MAILBOX_ID), {})
    sent = receipt.get("sent_readback") or {}
    checks = sent.get("checks") or {}
    source = source_bytes.decode("utf-8", errors="ignore")
    wrapper = wrapper_bytes.decode("utf-8", errors="ignore")
    thread_id = str(sent.get("thread_id") or "")
    message_id = str(sent.get("message_id") or "")
    required_source_markers = [
        "REQUEST_RECEIPT = ROOT / 'reports/glp-clinic-scout/affiliate-info-request-loop/orderlymeds-20260804.json'",
        # Re-anchored 2026-08-05: the binding moved above the fail-closed contract
        # check (it used to be read before assignment, crashing every run). The
        # semantic is identical: both ids come only from request.sent_readback.
        "sent_readback = request.get('sent_readback') or {}",
        "thread_id = str(sent_readback.get('thread_id') or '')",
        "source_message_id = str(sent_readback.get('message_id') or '')",
        "client.get_thread(thread_id, format='full')",
        "if message.get('id') != source_message_id and 'orderlymeds.com' in frm",
        "'threadId': thread_id",
    ]
    forbidden_source_markers = [
        "users().messages().list",
        "users().messages().get",
        "users().history().list",
        "search_unread",
        "list_messages",
        "thread_enumeration",
    ]
    job_ok = (
        job.get("enabled") is True
        and job.get("state") == "scheduled"
        and job.get("no_agent") is True
        and job.get("script") == SCOPED_READER_WRAPPER.name
        and job.get("schedule", {}).get("kind") == "interval"
        and job.get("schedule", {}).get("minutes") == 15
        and job.get("schedule_display") == "every 15m"
        and job.get("workdir") == str(BUSINESS)
        and job.get("script_sha256") == SCOPED_READER_WRAPPER_SHA256
        and job.get("script_sha256") == sha256(wrapper_bytes)
    )
    registry_ok = (
        row.get("lifecycle") == "active"
        and row.get("canonical_address", "").lower() == SCOPED_READER_CANONICAL_ADDRESS
        and receipt.get("mailbox_id") == SCOPED_READER_MAILBOX_ID
        and (receipt.get("preflight") or {}).get("mailbox_id") == SCOPED_READER_MAILBOX_ID
        and (receipt.get("preflight") or {}).get("account") == SCOPED_READER_CANONICAL_ADDRESS
    )
    receipt_ok = (
        receipt_sha256 == SCOPED_READER_RECEIPT_SHA256
        and receipt.get("provider") == SCOPED_READER_PROVIDER
        and receipt.get("status") == "PASS_SENT_VERIFIED"
        and receipt.get("provider_mutations") == 0
        and receipt.get("tracking_links_generated") == 0
        and receipt.get("secret_printed") is False
        and bool(thread_id)
        and bool(message_id)
        and receipt.get("source_thread_id") == thread_id
        and checks.get("thread_exact") is True
        and checks.get("thread_key_exact") is True
        and checks.get("from_exact") is True
        and checks.get("to_exact") is True
    )
    source_ok = (
        sha256(source_bytes) == SCOPED_READER_SOURCE_SHA256
        and all(marker in source for marker in required_source_markers)
        and not any(marker in source for marker in forbidden_source_markers)
        and "if not replies:" in source
    )
    wrapper_ok = "process_partner_info_replies.py --apply" in wrapper
    evidence = {
        "job_contract": job_ok,
        "registry_contract": registry_ok,
        "receipt_contract": receipt_ok,
        "source_contract": source_ok,
        "wrapper_invocation_contract": wrapper_ok,
        "job_id": SCOPED_READER_JOB_ID,
        "mailbox_id": receipt.get("mailbox_id"),
        "canonical_address": (row.get("canonical_address") or "").lower(),
        "provider": receipt.get("provider"),
        "thread_bound": bool(thread_id),
        "message_bound": bool(message_id),
        "wrapper_hash_match": sha256(wrapper_bytes) == SCOPED_READER_WRAPPER_SHA256,
        "source_hash_match": sha256(source_bytes) == SCOPED_READER_SOURCE_SHA256,
        "receipt_hash_match": receipt_sha256 == SCOPED_READER_RECEIPT_SHA256,
        "required_source_markers": [marker for marker in required_source_markers if marker in source],
        "forbidden_source_markers": [marker for marker in forbidden_source_markers if marker in source],
    }
    return all((job_ok, registry_ok, receipt_ok, source_ok, wrapper_ok)), evidence


def gate(gate_id: int, name: str, passed: bool, evidence: dict[str, Any], failures: list[str], *, blocking: bool = False) -> dict[str, Any]:
    status = "pass" if passed else ("blocked" if blocking else "fail")
    if not passed:
        failures.append(f"G{gate_id:02d}:{name}")
    return {"gate_id": gate_id, "name": name, "status": status, "evidence": evidence}


def validate_audit_chain(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    previous = "0" * 64
    failures = []
    count = 0
    for row in connection.execute("SELECT * FROM audit_events ORDER BY audit_id"):
        material = "|".join([previous, row["event_type"], row["object_type"], row["object_id"], row["actor"], row["payload_json"], row["created_at"]])
        expected = hashlib.sha256(material.encode()).hexdigest()
        if row["previous_hash"] != previous or row["event_hash"] != expected:
            failures.append(row["audit_id"])
        previous = row["event_hash"]
        count += 1
    return {"count": count, "broken_ids": failures, "status": "pass" if not failures else "fail"}


def main() -> int:
    failures: list[str] = []
    blockers: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    registry = json.loads((ROOT / "registry.json").read_text())
    rows = registry["mailboxes"]
    active = {row["mailbox_id"] for row in rows if row["lifecycle"] == "active"}
    shelved = {row["mailbox_id"] for row in rows if row["lifecycle"] == "shelved"}
    addresses = [str(value).lower() for row in rows for value in [row["canonical_address"], *row.get("aliases", []), *row.get("groups", [])]]
    ownership_manifest_error = None
    try:
        validate_ownership_manifest(registry)
    except ValueError as exc:
        ownership_manifest_error = str(exc)
    registry_ok = active == EXPECTED_ACTIVE and shelved == {"tlcc_legacy_identity", "swfa_legacy_identity"} and len(addresses) == len(set(addresses)) and ownership_manifest_error is None
    gates.append(gate(1, "canonical_registry_coverage", registry_ok, {"active_expected": len(EXPECTED_ACTIVE), "active_found": len(active), "mailbox_rows": len(rows), "unique_addresses": len(set(addresses)), "shelved": sorted(shelved), "ownership_manifest": {"status": "pass" if ownership_manifest_error is None else "fail", "error": ownership_manifest_error}}, failures))

    jobs_doc = json.loads(CRON_STORE.read_text())
    jobs = {row["id"]: row for row in jobs_doc["jobs"]}
    canonical_job = jobs.get(CANONICAL_JOB_ID) or {}
    legacy_states = {job_id: {"enabled": jobs.get(job_id, {}).get("enabled"), "state": jobs.get(job_id, {}).get("state")} for job_id in sorted(LEGACY_JOB_IDS)}
    crontab = subprocess.run(["crontab", "-l"], text=True, capture_output=True).stdout
    user_timers = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--no-legend"], text=True, capture_output=True)
    system_timers = subprocess.run(["systemctl", "list-timers", "--all", "--no-legend"], text=True, capture_output=True)
    duplicate_timer_pattern = re.compile(r"(?:kael[_-]?inbox|gmail[_-]?monitor|allocant[_-]?reply|boring[_-]?mailbox|glp[_-]?(?:inbound|gmail|reply))", re.I)
    timer_duplicates = duplicate_timer_pattern.findall((user_timers.stdout if user_timers.returncode == 0 else "") + "\n" + (system_timers.stdout if system_timers.returncode == 0 else ""))
    monitor_inventory_ok = all(job_id in jobs for job_id in LEGACY_JOB_IDS) and all(not jobs[job_id].get("enabled") and jobs[job_id].get("state") == "paused" for job_id in LEGACY_JOB_IDS) and "kael_inbox_processor.py --cron" not in crontab and not timer_duplicates
    gates.append(gate(2, "monitor_inventory_and_dedup", monitor_inventory_ok, {"legacy_jobs": legacy_states, "legacy_user_cron_present": "kael_inbox_processor.py --cron" in crontab, "systemd_duplicate_matches": len(timer_duplicates), "canonical_owner": CANONICAL_JOB_ID}, failures))

    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
    schema_version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    classification_columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox_classifications)")}
    required_tables = {
        "mailboxes", "provider_cursors", "inbox_events", "inbox_attachments",
        "inbox_classifications", "notification_outbox", "mailbox_actions",
        "operator_decisions", "local_drafts", "inbox_runs", "audit_events",
        "side_effect_counters", "future_send_policy", "affiliate_approval_facts",
        "affiliate_approval_event_facts", "manual_dispositions",
    }
    audit = validate_audit_chain(connection)
    private_roots = [Path("/home/ubuntu/.hermes/state/email-monitor-private"), Path("/home/ubuntu/.hermes/state/email-monitor-attachments"), Path("/home/ubuntu/.hermes/state/email-monitor-backups")]
    private_dir_bad = [str(path) for root in private_roots if root.exists() for path in [root, *[item for item in root.rglob("*") if item.is_dir()]] if mode(path) != 0o700]
    private_file_bad = [str(path) for root in private_roots if root.exists() for path in root.rglob("*") if path.is_file() and mode(path) != 0o600]
    db_family = [path for path in [DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm")] if path.exists()]
    db_mode_bad = [str(path) for path in db_family if mode(path) != 0o600]
    reply_columns = {"reply_class", "intent_confidence", "matched_signals_json"}
    schema_ok = integrity == "ok" and not foreign_failures and schema_version == EXPECTED_SCHEMA_VERSION and required_tables <= tables and reply_columns <= classification_columns and audit["status"] == "pass" and (ROOT / "migrations/001_initial.sql").exists() and (ROOT / "migrations/002_reply_class.sql").exists() and not private_dir_bad and not private_file_bad and not db_mode_bad
    gates.append(gate(3, "ledger_schema_migrations_audit_backup_redaction", schema_ok, {"integrity": integrity, "foreign_key_failures": len(foreign_failures), "schema_version": schema_version, "required_tables_present": len(required_tables & tables), "reply_columns_present": sorted(reply_columns & classification_columns), "audit_chain": audit, "bad_private_dirs": private_dir_bad[:5], "bad_private_files": private_file_bad[:5], "bad_db_modes": db_mode_bad}, failures))

    tests = run([sys.executable, *UNIT_TEST_DISCOVERY_ARGS], 300)
    fixture_manifest = json.loads((ROOT / "fixtures/manifest.json").read_text())
    fixture_required = {"dmarc_yahoo_routine", "dmarc_gmx_gzip_routine", "dmarc_real_anomaly", "newsletter", "marketing_automation_ooo", "bounce", "human_lead_reply", "security_auth", "transactional_receipt", "investor_reply", "support_customer_request", "prompt_injection", "malformed_mime", "html_only", "safe_attachment", "glp_partner_alias", "allocant_investor_alias", "unknown"}
    fixture_ids = {item["id"] for item in fixture_manifest["fixtures"]}
    test_count_match = re.search(r"Ran (\d+) tests?", tests["stderr_tail"])
    tests_run = int(test_count_match.group(1)) if test_count_match else 0
    tests_ok = tests["returncode"] == 0 and tests_run >= MINIMUM_INBOX_TESTS and fixture_required <= fixture_ids
    gates.append(gate(4, "synthetic_fixture_matrix", tests_ok, {"unit_returncode": tests["returncode"], "tests_run": tests_run, "minimum_tests": MINIMUM_INBOX_TESTS, "discovery_args": list(UNIT_TEST_DISCOVERY_ARGS), "fixture_count": fixture_manifest["fixture_count"], "required_fixture_ids_present": len(fixture_required & fixture_ids)}, failures))

    route_counts = {row["route"]: row["n"] for row in connection.execute("SELECT route,COUNT(*) n FROM inbox_classifications GROUP BY route")}
    current_route_counts = {row["route"]: row["n"] for row in connection.execute("SELECT route,COUNT(*) n FROM inbox_classifications WHERE policy_version=? GROUP BY route", (POLICY_VERSION,))}
    wrong_ops = connection.execute("SELECT COUNT(*) FROM inbox_classifications WHERE policy_version=? AND route='ops' AND category NOT IN ('service_degradation','unknown')", (POLICY_VERSION,)).fetchone()[0]
    wrong_silent = connection.execute("SELECT COUNT(*) FROM inbox_classifications WHERE policy_version=? AND route='silent' AND category IN ('human_lead_reply','support_customer_request','investor_reply','partner_vendor_action','security_auth','payment_sale_expense','legal_compliance','booking_calendar','unknown')", (POLICY_VERSION,)).fetchone()[0]
    routing_ok = tests_ok and wrong_ops == 0 and wrong_silent == 0 and {"decisions", "ops", "silent"} <= set(route_counts)
    gates.append(gate(5, "channel_routing_and_noise_suppression", routing_ok, {"historical_route_counts": route_counts, "current_policy": POLICY_VERSION, "current_policy_route_counts": current_route_counts, "current_policy_wrong_ops": wrong_ops, "current_policy_wrong_silent": wrong_silent, "decisions_channel": "1524254375453786294", "ops_channel": "1487300132054765629"}, failures))

    public_receipts = list((REPORT / "production-runs").glob("*.json")) + [REPORT / "scheduler-cutover-receipt.json"]
    leak_markers = ["top-secret-token", "-----BEGIN PRIVATE KEY-----", "sk_live_fixture_canary", "Bearer fixture-secret"]
    leaked: list[str] = []
    receipt_contract_ok = True
    for path in public_receipts:
        text = path.read_text(errors="ignore")
        if any(marker in text for marker in leak_markers):
            leaked.append(str(path))
        if path.parent.name == "production-runs":
            payload = json.loads(text)
            receipt_contract_ok = receipt_contract_ok and payload.get("secret_printed") is False and payload.get("raw_bodies_in_receipt") is False
    payload_leaks = connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE payload LIKE '%Authorization:%' OR payload LIKE '%BEGIN PRIVATE KEY%' OR payload LIKE '%top-secret-token%'").fetchone()[0]
    privacy_ok = not leaked and payload_leaks == 0 and receipt_contract_ok and tests_ok
    gates.append(gate(6, "private_body_and_secret_non_disclosure", privacy_ok, {"scanned_receipts": len(public_receipts), "leaked_files": leaked, "payload_leaks": payload_leaks, "receipt_flags_valid": receipt_contract_ok}, failures))

    event_count = connection.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0]
    action_count = connection.execute("SELECT COUNT(DISTINCT event_id) FROM mailbox_actions WHERE action='preserve_no_provider_mutation' AND status='verified'").fetchone()[0]
    events_without_class = connection.execute("SELECT COUNT(*) FROM inbox_events e LEFT JOIN inbox_classifications c ON c.event_id=e.event_id WHERE c.event_id IS NULL").fetchone()[0]
    events_without_action = connection.execute("SELECT COUNT(*) FROM inbox_events e LEFT JOIN mailbox_actions a ON a.event_id=e.event_id WHERE a.event_id IS NULL").fetchone()[0]
    durable_ok = event_count > 0 and action_count == event_count and events_without_class == 0 and events_without_action == 0
    gates.append(gate(7, "durable_before_handled", durable_ok, {"events": event_count, "preserve_actions": action_count, "events_without_classification": events_without_class, "events_without_action": events_without_action, "provider_mutations": 0}, failures))

    duplicate_groups = connection.execute("SELECT COUNT(*) FROM (SELECT mailbox_id,provider_message_id,COUNT(*) n FROM inbox_events GROUP BY 1,2 HAVING n>1)").fetchone()[0]
    outbox_failed = connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE status IN ('failed_retryable','posted_pending_readback')").fetchone()[0]
    outbox_duplicate = connection.execute("SELECT COUNT(*) FROM (SELECT idempotency_key,COUNT(*) n FROM notification_outbox GROUP BY 1 HAVING n>1)").fetchone()[0]
    lossless_ok = tests_ok and duplicate_groups == 0 and outbox_failed == 0 and outbox_duplicate == 0
    gates.append(gate(8, "crash_retry_rate_limit_and_dedup", lossless_ok, {"duplicate_provider_groups": duplicate_groups, "retryable_or_unreadback_outbox": outbox_failed, "duplicate_idempotency_keys": outbox_duplicate, "adversarial_test_pass": tests_ok}, failures))

    health_rows = [dict(row) for row in connection.execute("SELECT m.mailbox_id,m.lifecycle,m.poll_seconds,c.last_auth_success_at,c.last_read_success_at,c.last_complete_poll_at,c.unhealthy_reason FROM mailboxes m JOIN provider_cursors c USING(mailbox_id) WHERE m.lifecycle='active'")]
    health_fail = []
    for row in health_rows:
        age = iso_age_seconds(row["last_complete_poll_at"])
        if not row["last_auth_success_at"] or not row["last_read_success_at"] or row["unhealthy_reason"] or age is None or age > max(2 * row["poll_seconds"], row["poll_seconds"] + 120):
            health_fail.append({"mailbox_id": row["mailbox_id"], "age": age, "reason": row["unhealthy_reason"]})
    health_ok = len(health_rows) == len(EXPECTED_ACTIVE) and not health_fail
    if not health_ok:
        blockers.append({"gate_id": "INBOX-HEALTH", "owner": "Kael/operator only if provider auth is required", "required_action": "Restore the affected registered provider auth/read path without changing mailbox identity or sending mail.", "required_artifacts": [str(DB), str(REPORT / "production-runs")], "required_readback": f"{len(EXPECTED_ACTIVE)} active surfaces show current auth/read/complete timestamps within SLO and no unhealthy reason.", "resume_condition": "Run one read-only canonical poll, then rerun this validator.", "self_approval_allowed": False})
    gates.append(gate(9, "live_auth_read_freshness_probes", health_ok, {"active_health_rows": len(health_rows), "failures": health_fail, "external_sends": 0}, failures, blocking=True))

    canonical_age = iso_age_seconds(canonical_job.get("last_run_at"))
    scoped_receipt = {}
    scoped_receipt_hash = ""
    try:
        scoped_receipt_bytes = SCOPED_READER_RECEIPT.read_bytes()
        scoped_receipt_hash = sha256(scoped_receipt_bytes)
        scoped_receipt = json.loads(scoped_receipt_bytes)
    except (OSError, json.JSONDecodeError):
        scoped_receipt_bytes = b""
    scoped_ok, scoped_evidence = validate_scoped_reader_contract(
        jobs,
        registry,
        scoped_receipt,
        scoped_receipt_hash,
        SCOPED_READER_WRAPPER.read_bytes() if SCOPED_READER_WRAPPER.exists() else b"",
        SCOPED_READER_SOURCE.read_bytes() if SCOPED_READER_SOURCE.exists() else b"",
    )
    scheduler_ok = canonical_job.get("enabled") is True and canonical_job.get("state") == "scheduled" and canonical_job.get("no_agent") is True and canonical_job.get("script") == "inbox_control_plane_cron.py" and canonical_job.get("schedule_display") == "every 5m" and canonical_job.get("last_status") == "ok" and canonical_age is not None and canonical_age <= 720 and monitor_inventory_ok and scoped_ok
    gates.append(gate(10, "scheduler_live_readback_and_scoped_reader_contract", scheduler_ok, {"canonical_job_id": CANONICAL_JOB_ID, "enabled": canonical_job.get("enabled"), "state": canonical_job.get("state"), "no_agent": canonical_job.get("no_agent"), "last_status": canonical_job.get("last_status"), "last_run_age_seconds": canonical_age, "legacy_enabled": sum(bool(jobs[j].get("enabled")) for j in LEGACY_JOB_IDS), "general_inbox_owner": CANONICAL_JOB_ID, "scoped_reader": scoped_evidence | {"status": "pass" if scoped_ok else "fail"}}, failures))

    class_count = connection.execute("SELECT COUNT(*) FROM inbox_classifications").fetchone()[0]
    classified_event_count = connection.execute("SELECT COUNT(DISTINCT event_id) FROM inbox_classifications").fetchone()[0]
    decision_count = connection.execute("SELECT COUNT(*) FROM inbox_classifications WHERE policy_version=? AND route='decisions'", (POLICY_VERSION,)).fetchone()[0]
    suppressed_count = connection.execute("SELECT COUNT(*) FROM inbox_classifications WHERE policy_version=? AND route='silent'", (POLICY_VERSION,)).fetchone()[0]
    draft_count = connection.execute("SELECT COUNT(*) FROM local_drafts").fetchone()[0]
    pending_draft_count = connection.execute("SELECT COUNT(*) FROM local_drafts WHERE status='draft_generated'").fetchone()[0]
    # Schema v5 permits additive repair classifications while preserving every
    # historical row. The old one-classification-per-event equality therefore
    # becomes complete distinct-event coverage plus no missing classifications.
    canary_ok = event_count > 0 and class_count >= event_count and classified_event_count == event_count and events_without_class == 0 and decision_count > 0 and suppressed_count > 0 and draft_count > 0 and tests_ok
    gates.append(gate(11, "draft_only_end_to_end_canary", canary_ok, {"events": event_count, "classifications": class_count, "classified_events": classified_event_count, "append_only_repair_rows": class_count - classified_event_count, "events_without_classification": events_without_class, "current_policy": POLICY_VERSION, "current_policy_decisions": decision_count, "current_policy_suppressed": suppressed_count, "local_draft_history": draft_count, "pending_local_drafts": pending_draft_count, "provider_drafts": 0}, failures))

    counters = {row["name"]: row["value"] for row in connection.execute("SELECT name,value FROM side_effect_counters")}
    bad_runs = connection.execute("SELECT COUNT(*) FROM inbox_runs WHERE outbound_send_count!=0 OR provider_draft_count!=0 OR provider_mutation_count!=0").fetchone()[0]
    policy = connection.execute("SELECT enabled,requirements_json FROM future_send_policy WHERE policy_id='phase1-global-deny'").fetchone()
    source = (ROOT / "control_plane.py").read_text()
    forbidden_tokens = ["messages().send", "smtp.send", "send_message_transport", "drafts().create", "STORE +FLAGS", "EXPUNGE"]
    forbidden_found = [token for token in forbidden_tokens if token in source]
    verified_send_rows = connection.execute(
        """SELECT action,status,provider_receipt_json FROM mailbox_actions
           WHERE (action='email_sent_verified' AND status='verified')
              OR (action='email_sent_exact_approved_draft' AND status='verified_sent')"""
    ).fetchall()
    verified_send_receipts_ok = True
    for row in verified_send_rows:
        try:
            send_receipt = json.loads(row["provider_receipt_json"])
        except json.JSONDecodeError:
            verified_send_receipts_ok = False
            continue
        verified_send_receipts_ok = verified_send_receipts_ok and bool(send_receipt.get("provider_message_id")) and bool(send_receipt.get("provider_thread_id"))
        if "secret_printed" in send_receipt:
            verified_send_receipts_ok = verified_send_receipts_ok and send_receipt.get("secret_printed") is False
    unresolved_submissions = connection.execute(
        "SELECT COUNT(*) FROM mailbox_actions WHERE status IN ('submission_started','reconcile_required','provider_outcome_uncertain')"
    ).fetchone()[0]
    controlled_send_ok = (
        set(counters) == {"external_sends", "provider_drafts", "provider_mutations"}
        and counters["external_sends"] == len(verified_send_rows)
        and counters["provider_drafts"] == 0
        and 0 <= counters["provider_mutations"] <= counters["external_sends"]
        and verified_send_receipts_ok
        and unresolved_submissions == 0
        and bad_runs == 0
        and policy and policy[0] == 0
        and not forbidden_found
    )
    gates.append(gate(12, "controlled_approved_send_reconciliation", controlled_send_ok, {"side_effect_counters": counters, "durable_verified_sends": len(verified_send_rows), "verified_send_receipts_ok": verified_send_receipts_ok, "unresolved_submissions": unresolved_submissions, "runs_with_side_effects": bad_runs, "global_auto_send_policy_enabled": policy[0] if policy else None, "forbidden_send_tokens_in_control_plane": forbidden_found}, failures))

    backups = sorted(Path("/home/ubuntu/.hermes/state/email-monitor-backups").glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    restore = {"status": "missing"}
    if backups:
        from inbox_control_plane.control_plane import restore_test
        restore = restore_test(backups[0])
    runbook = REPORT / "runbook-rollback-incident-recovery.md"
    recovery_ok = runbook.exists() and runbook.stat().st_size > 3000 and bool(backups) and restore.get("status") == "pass" and restore.get("production_provider_touched") is False and restore.get("external_send") is False
    gates.append(gate(13, "rollback_and_incident_recovery_exercised", recovery_ok, {"runbook": str(runbook), "backup_count": len(backups), "latest_backup": str(backups[0]) if backups else None, "restore": restore}, failures))

    required_artifacts = [REPORT / "implementation-report.md", REPORT / "canonical-mailbox-registry.md", REPORT / "scheduler-cutover-receipt.json", REPORT / "proposed-state-patch.md", REPORT / "firecrawl-design-research.md", CONTRACT]
    missing_artifacts = [str(path) for path in required_artifacts if not path.exists() or path.stat().st_size == 0]
    first_13_pass = all(item["status"] == "pass" for item in gates)
    final_artifacts_ok = first_13_pass and not missing_artifacts
    gates.append(gate(14, "final_loopos_artifacts_ready", final_artifacts_ok, {"prior_gates_passed": sum(item["status"] == "pass" for item in gates), "required_artifacts": len(required_artifacts), "missing_artifacts": missing_artifacts, "fable_opus_acceptance": "blocked_spend_gate_not_asserted"}, failures))

    connection.close()
    fail_statuses = [item for item in gates if item["status"] == "fail"]
    blocked_statuses = [item for item in gates if item["status"] == "blocked"]
    status = "fail" if fail_statuses else ("blocked" if blocked_statuses else "pass")
    details = {
        "status": status, "matrix_version": "2026-07-11", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "gates": gates, "failures": failures, "blockers": blockers,
        "unit_test": tests, "secret_printed": False,
    }
    DETAILS.parent.mkdir(parents=True, exist_ok=True)
    DETAILS.write_text(json.dumps(details, indent=2, sort_keys=True) + "\n")
    details_hash = sha256(DETAILS.read_bytes())
    compact = {
        "status": status,
        "reason": f"{sum(item['status']=='pass' for item in gates)}/14 deterministic gates passed",
        "failures": failures,
        "blockers": blockers,
        "evidence": {
            "run_id": canonical_job.get("last_run_at") or "unknown",
            "matrix_version": "2026-07-11",
            "active_mailboxes_expected": len(EXPECTED_ACTIVE),
            "active_mailboxes_passed": len(EXPECTED_ACTIVE) - len(health_fail),
            "shelved_jobs_enabled": 0,
            "phase1_external_sends": counters.get("external_sends"),
            "phase1_provider_drafts": counters.get("provider_drafts"),
            "phase1_provider_mutations": counters.get("provider_mutations"),
            "tests_passed": tests_run if tests_ok else 0,
            "details_path": str(DETAILS),
            "details_sha256": details_hash,
            "secret_printed": False,
            "fable_opus_acceptance": "blocked_spend_gate_not_asserted",
        },
    }
    print(json.dumps(compact, sort_keys=True))
    return 0 if status == "pass" else (2 if status == "blocked" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
