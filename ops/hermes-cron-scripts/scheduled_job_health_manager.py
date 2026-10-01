#!/usr/bin/env python3
"""Tolerant scheduled-job health manager for Kael/Hermes.

Silent on healthy runs. On failed/misfired scheduled jobs:
- investigate stored cron metadata and latest output artifacts
- classify root cause with circuit-breaker policy
- queue one safe rerun only for low-risk classes / explicit allowlists
- otherwise print a Discord-ready report with exact blocker + fix path
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

sys.path.insert(0, '/home/ubuntu/business/scripts')
import scheduled_source_drift

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import (
    title as fmt_title, kv as fmt_kv, kvline as fmt_kvline, section as fmt_section,
    meaning as fmt_meaning, subtext as fmt_subtext, render as fmt_render,
)

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print(f"Scheduled Job Health Manager failed: PyYAML unavailable: {exc}")
    raise

HOME = Path.home()
BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "ops" / "operator-console"))
import pushover_client  # noqa: F401,E402 - operator RED-alert wiring/readiness
import red_alert_hook  # noqa: E402
import monitor_heartbeats  # noqa: E402
import n8n_execution_budget  # noqa: E402
JOBS_PATH = HOME / ".hermes" / "cron" / "jobs.json"
OUTPUT_ROOT = HOME / ".hermes" / "cron" / "output"
REGISTRY_PATH = BUSINESS / "ops" / "kael-mission-registry.yaml"
SIDE_EFFECT_PATH = BUSINESS / "ops" / "side-effect-classes.yaml"
POLICY_PATH = BUSINESS / "ops" / "circuit-breakers.yaml"
STATE_PATH = BUSINESS / "ops" / "scheduled-job-health-state.json"
LEDGER_PATH = BUSINESS / "ops" / "kael-run-ledger.jsonl"
REPORT_BASE = BUSINESS / "reports" / "scheduled-job-health"
GLP_OBSERVABILITY_JOB_ID = "42391e23a232"
GLP_OBSERVABILITY_RECEIPTS = (
    BUSINESS
    / "reports/glp-clinic-scout/operator-national-wow-20260723/receipts/collector-runs"
)
DELIVERY_DIR = BUSINESS / "reports" / "claude-audit-20260707" / "operator-delivery-20260708"
NIGHTLY_UPDATE_LAUNCH_ROOT = BUSINESS / "reports" / "hermes-update-repair" / "service-launches"
SCRIPT_NAME = "scheduled_job_health_manager"

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)(\s*[:=]\s*)([^\s]+)"),
    re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"fc-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{12,}"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utc_now()
    return dt.isoformat().replace("+00:00", "Z")


def redact(text: Any) -> str:
    s = "" if text is None else str(text)
    s = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", s)
    for rx in SECRET_PATTERNS[1:]:
        s = rx.sub("[REDACTED]", s)
    return s


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1] + "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or default
    except Exception as exc:
        return {"_load_error": str(exc), **(default if isinstance(default, dict) else {})}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_ledger(event: dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: redact(v) if isinstance(v, str) else v for k, v in event.items()}
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, sort_keys=True, ensure_ascii=False) + "\n")


def schedule_text(schedule: Any) -> str:
    if isinstance(schedule, dict):
        if schedule.get("kind") == "cron":
            return schedule.get("expr") or json.dumps(schedule, sort_keys=True)
        if schedule.get("kind") == "interval":
            return f"every {schedule.get('minutes')}m"
        if schedule.get("kind") == "once":
            return str(schedule.get("at") or schedule.get("time") or schedule)
        return json.dumps(schedule, sort_keys=True)
    return str(schedule or "")


def job_list() -> list[dict[str, Any]]:
    data = load_json(JOBS_PATH, {})
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)]


def build_registry_map() -> dict[str, dict[str, Any]]:
    reg = load_yaml(REGISTRY_PATH, {})
    out: dict[str, dict[str, Any]] = {}
    for m in reg.get("missions", []) if isinstance(reg, dict) else []:
        if not isinstance(m, dict):
            continue
        source = str(m.get("source_id") or "")
        mid = str(m.get("id") or "")
        if source:
            out[source] = m
        if mid.startswith("hermes-cron-"):
            out[mid.replace("hermes-cron-", "", 1)] = m
    return out


def should_exclude(job: dict[str, Any], policy: dict[str, Any]) -> bool:
    name = str(job.get("name") or "")
    script = str(job.get("script") or "")
    prompt = str(job.get("prompt") or "")
    hay = f"{name}\n{script}\n{prompt}".lower()
    excludes = policy.get("exclude_jobs", {}) if isinstance(policy, dict) else {}
    for frag in excludes.get("name_contains", []) or []:
        if str(frag).lower() in name.lower():
            return True
    for frag in excludes.get("script_contains", []) or []:
        if str(frag).lower() in hay:
            return True
    return SCRIPT_NAME in hay


def latest_output_text(job_id: str, max_files: int, max_bytes: int, recent_hours: int, include_old: bool) -> tuple[str, list[str]]:
    job_dir = OUTPUT_ROOT / job_id
    if not job_dir.exists():
        return "", []
    now_ts = utc_now().timestamp()
    files = sorted([p for p in job_dir.glob("*.md") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    chunks: list[str] = []
    paths: list[str] = []
    for p in files:
        try:
            age_hours = (now_ts - p.stat().st_mtime) / 3600
            if age_hours > recent_hours and not include_old:
                continue
            b = p.read_bytes()
            chunks.append(b[-max_bytes:].decode("utf-8", errors="replace"))
            paths.append(str(p))
        except Exception as exc:
            chunks.append(f"[could not read output file {p}: {exc}]")
            paths.append(str(p))
    return "\n\n---OUTPUT FILE---\n\n".join(chunks), paths


def output_text_for_patterns(text: str, status: str) -> str:
    """Return only the execution-result portion of Hermes cron markdown.

    Cron output files can embed the full prompt plus loaded skill text before the
    actual `## Response` or `## Error` section. Scanning the whole file creates
    false positives when a skill/runbook contains examples like "HTTP 401" or
    "RuntimeError" even though the job succeeded. For no-agent/script logs that
    have no markdown sections, fall back to the raw text.
    """
    chunks = text.split("\n\n---OUTPUT FILE---\n\n") if text else []
    relevant: list[str] = []
    ok_status = status.lower() in {"ok", "success"}
    for chunk in chunks:
        if not chunk:
            continue
        error_match = list(re.finditer(r"(?m)^## Error\s*$", chunk))
        response_match = list(re.finditer(r"(?m)^## Response\s*$", chunk))
        if error_match:
            relevant.append(chunk[error_match[-1].start():])
        elif response_match:
            relevant.append(chunk[response_match[-1].start():])
        elif "\n## Prompt\n" in chunk or chunk.startswith("## Prompt\n"):
            # Prompt-only markdown contains no execution result; keep just the
            # short header/status area rather than scanning embedded skills.
            relevant.append("\n".join(chunk.splitlines()[:12]))
        elif ok_status:
            relevant.append(re.sub(r"\(No response generated\)|No response generated", "", chunk, flags=re.I))
        else:
            relevant.append(chunk)
    return "\n\n---OUTPUT FILE---\n\n".join(relevant)


def output_scan_limit(status: str, delivery_error: str, configured_max_files: int) -> int:
    """Scan only the latest artifact after a confirmed successful run.

    Older failures are useful when the scheduler still reports failure, but once
    both execution and delivery are healthy they are resolved history. Scanning
    them kept repaired jobs incident-open until enough later runs displaced the
    old artifact.
    """
    healthy = status.lower() in {"ok", "success", "none", "null"}
    return 1 if healthy and not delivery_error.strip() else max(1, configured_max_files)


@dataclass
class Issue:
    job_id: str
    job_name: str
    schedule: str
    side_effect_class: str
    classification: str
    severity: str
    reason: str
    evidence: str
    operator_fix: str
    can_auto_fix: bool
    safe_to_rerun: bool
    action: str = "alert"
    command_output: str = ""
    output_paths: list[str] = field(default_factory=list)
    incident_key: str = ""
    alert_min_interval_minutes: int | None = None
    alert_once: bool = False


def pattern_issues(job: dict[str, Any], side_class: str, text: str, paths: list[str], policy: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    job_id = str(job.get("id") or job.get("job_id") or "")
    name = str(job.get("name") or job_id)
    sched = schedule_text(job.get("schedule"))
    status = str(job.get("last_status") or "")
    # Empty response alone is not a failure for ok/script-backed jobs. Also do
    # not scan embedded prompt/skill bodies; they often contain example error
    # strings that are not evidence from this run.
    text_for_patterns = output_text_for_patterns(text, status)
    for pat in policy.get("known_patterns", []) or []:
        rx = pat.get("regex")
        if not rx:
            continue
        try:
            match = re.search(rx, text_for_patterns, flags=re.I | re.M)
        except re.error:
            continue
        if not match:
            continue
        classification = str(pat.get("classification") or pat.get("id") or "known_pattern")
        evidence = redact(match.group(0))[:240]
        can_auto_fix = bool(pat.get("can_auto_fix", False))
        issues.append(Issue(
            job_id=job_id,
            job_name=redact(name),
            schedule=sched,
            side_effect_class=side_class,
            classification=classification,
            severity=str(pat.get("severity") or "yellow"),
            reason=str(pat.get("id") or classification),
            evidence=evidence,
            operator_fix=str(pat.get("operator_fix") or "Inspect latest cron output and repair the job-specific cause."),
            can_auto_fix=can_auto_fix,
            safe_to_rerun=False,
            output_paths=paths,
        ))
    return issues


def schedule_validation_issue(job: dict[str, Any], side_class: str) -> Issue | None:
    sched = job.get("schedule")
    job_id = str(job.get("id") or job.get("job_id") or "")
    name = str(job.get("name") or job_id)
    if not isinstance(sched, dict):
        return Issue(job_id, redact(name), schedule_text(sched), side_class, "schedule_parse_broken", "red", "schedule_not_dict", redact(str(sched))[:240], "Update this job to a valid Hermes schedule object / recreate with a valid cron or interval schedule.", False, False)
    if sched.get("kind") == "cron" and not sched.get("expr"):
        return Issue(job_id, redact(name), schedule_text(sched), side_class, "schedule_parse_broken", "red", "cron_schedule_missing_expr", redact(str(sched))[:240], f"Run: hermes cron update --job-id {job_id} --schedule '<valid cron expr>'", False, False)
    return None


def collector_overlap_success_receipt(job: dict[str, Any]) -> Path | None:
    """Reconcile a duplicate-fire lock result with the successful lock owner."""
    if str(job.get("id") or "") != GLP_OBSERVABILITY_JOB_ID:
        return None
    if "collector lock is already held" not in str(job.get("last_error") or ""):
        return None
    duplicate_at = parse_dt(job.get("last_run_at"))
    if duplicate_at is None or not GLP_OBSERVABILITY_RECEIPTS.is_dir():
        return None
    receipts = sorted(
        GLP_OBSERVABILITY_RECEIPTS.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:10]
    for path in receipts:
        payload = load_json(path, {})
        if not isinstance(payload, dict) or str(payload.get("status") or "").lower() != "pass":
            continue
        started = parse_dt(payload.get("started_at_utc"))
        finished = parse_dt(payload.get("finished_at_utc"))
        if started and finished and started <= duplicate_at <= finished:
            return path
    return None


def fix_landed_after_failure(job: dict[str, Any]) -> Path | None:
    """AI-67: a job whose FIX landed after its last failure is not a live defect.

    Cause, 2026-08-22/23: the repeat-offender enforcement lane repaired and hash-re-pinned
    several jobs at ~02:14Z. Those jobs had not RUN again yet, so `last_status` still read
    `error` from a pre-fix run and this watchdog paged an operator card every 15 minutes for
    failures that were already fixed. Four distinct jobs cycled that way in one night, which
    is exactly how real alerts get ignored.

    Rule: if a receipt/fix artifact for this job id is NEWER than the job's last run, the
    honest state is "fix landed, next run proves it" — not manual attention. If the NEXT run
    fails, `last_run_at` moves past the receipt and this suppression stops applying, so a
    genuine regression still alerts loudly.
    """
    last_run = parse_dt(job.get("last_run_at"))
    if last_run is None:
        return None
    job_id = str(job.get("id") or job.get("job_id") or "")
    if not job_id:
        return None
    roots = [
        BUSINESS / "reports/ops/REPEAT-OFFENDER-ENFORCEMENT-20260822",
        BUSINESS / "reports/ops/error-fixer/deep-fix",
        BUSINESS / "reports/hermes-update-repair/service-launches",
    ]
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) <= last_run:
                        continue
                    if path.stat().st_size > 4_000_000:
                        continue
                    if job_id in path.name or job_id in path.read_text(errors="ignore"):
                        return path
                except Exception:
                    continue
        except Exception:
            continue
    return None


def generic_status_issues(job: dict[str, Any], side_class: str, now: datetime, policy: dict[str, Any], paths: list[str]) -> list[Issue]:
    out: list[Issue] = []
    job_id = str(job.get("id") or job.get("job_id") or "")
    name = str(job.get("name") or job_id)
    sched = schedule_text(job.get("schedule"))
    status = str(job.get("last_status") or "").lower()
    delivery_error = str(job.get("last_delivery_error") or "").strip()
    overlap_receipt = collector_overlap_success_receipt(job)
    fix_receipt = fix_landed_after_failure(job)
    if (status and status not in {"ok", "success", "none", "null"}
            and overlap_receipt is None and fix_receipt is None):
        out.append(Issue(job_id, redact(name), sched, side_class, "generic_cron_failure", "red", f"last_status={status}", redact(status), "Inspect latest output. Safe read/report jobs can be queued once; external write jobs require manual approval/readback before rerun.", True, False, output_paths=paths))
    if delivery_error:
        out.append(Issue(job_id, redact(name), sched, side_class, "delivery_error", "red", "last_delivery_error_present", redact(delivery_error[:300]), "Fix the delivery target/channel/auth or reduce message size; then rerun only if duplicate-safe.", False, False, output_paths=paths))
    next_run = parse_dt(job.get("next_run_at"))
    grace = int(policy.get("defaults", {}).get("stale_due_grace_minutes", 45))
    if bool(job.get("enabled", True)) and next_run and now - next_run > timedelta(minutes=grace):
        age_min = int((now - next_run).total_seconds() // 60)
        out.append(Issue(job_id, redact(name), sched, side_class, "stale_due_job", "yellow", f"next_run_at is {age_min}m in the past", str(job.get("next_run_at")), "Check Hermes cron tick/lock/scheduler health. Safe jobs may be queued once; if many jobs are stale, diagnose scheduler before mass reruns.", True, False, output_paths=paths))
    return out


def side_effect_class(job: dict[str, Any], regmap: dict[str, dict[str, Any]]) -> str:
    job_id = str(job.get("id") or job.get("job_id") or "")
    text = " ".join(str(job.get(k) or "") for k in ["name", "prompt", "script", "deliver"])
    t = text.lower()
    # Higher-risk keyword overrides beat old registry guesses. This prevents ads/payment jobs
    # from being treated as merely "external message possible" because they also report to Discord.
    if any(x in t for x in ["refund", "stripe_api_write", "payment"]):
        return "payment_or_refund"
    if any(x in t for x in ["google ads", "meta ads", "meta graph", "optmyzr", "ads optimizer", "ad account", "google campaign", "facebook campaign"]):
        return "ads_or_budget_mutation"
    if job_id in regmap:
        return str(regmap[job_id].get("side_effect_class") or "unknown_review_before_mutation")
    if any(x in t for x in ["reply", "post", "send_message", "discord", "email", "gmail", "xurl", "schedule_content"]):
        return "external_message_or_publish_possible"
    if any(x in t for x in ["monitor", "watchdog", "audit", "check", "report", "digest"]):
        return "read_report_monitor"
    return "unknown_review_before_mutation"


def fill_incident_keys(issues: list[Issue]) -> None:
    for issue in issues:
        if issue.incident_key:
            continue  # producer-owned stable episode key, not changing evidence
        fp_src = "|".join([issue.job_id, issue.classification, issue.reason, issue.evidence[:160]])
        fp = hashlib.sha1(fp_src.encode("utf-8", errors="replace")).hexdigest()[:16]
        issue.incident_key = f"{issue.job_id}:{issue.classification}:{fp}"


def decide_and_maybe_rerun(issue: Issue, state: dict[str, Any], side_policy: dict[str, Any], policy: dict[str, Any], dry_run: bool) -> Issue:
    safe_classes = set(side_policy.get("safe_auto_rerun_classes", []) or [])
    never_classes = set(side_policy.get("never_auto_rerun_classes", []) or [])
    overrides = policy.get("job_overrides", {}) or {}
    override = overrides.get(issue.job_id, {}) if isinstance(overrides, dict) else {}
    max_reruns = int(override.get("max_auto_reruns_per_incident") or policy.get("defaults", {}).get("max_auto_reruns_per_incident", 1))
    cooldown = int(policy.get("defaults", {}).get("rerun_cooldown_minutes", 120))
    incident = state.setdefault("incidents", {}).setdefault(issue.incident_key, {})
    reruns = int(incident.get("reruns", 0) or 0)
    last_rerun = parse_dt(incident.get("last_rerun_at"))
    cooldown_ok = not last_rerun or utc_now() - last_rerun > timedelta(minutes=cooldown)
    explicit_allow = bool(override.get("auto_rerun"))
    explicit_deny = override.get("auto_rerun") is False
    safe = (issue.side_effect_class in safe_classes and issue.side_effect_class not in never_classes) or explicit_allow
    if explicit_deny:
        safe = False
    issue.safe_to_rerun = bool(safe and issue.can_auto_fix)
    if not issue.safe_to_rerun:
        issue.action = "alert_no_rerun"
        return issue
    if reruns >= max_reruns:
        issue.action = "alert_rerun_cap_reached"
        return issue
    if not cooldown_ok:
        issue.action = "alert_rerun_cooldown"
        return issue
    if dry_run:
        issue.action = "would_queue_safe_rerun"
        return issue
    cmd = ["hermes", "cron", "run", issue.job_id]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        issue.command_output = redact((proc.stdout or "") + (proc.stderr or ""))[:1200]
        if proc.returncode == 0:
            issue.action = "queued_safe_rerun"
            incident["reruns"] = reruns + 1
            incident["last_rerun_at"] = iso()
            append_ledger({"ts": iso(), "event": "scheduled_job_safe_rerun_queued", "job_id": issue.job_id, "job_name": issue.job_name, "classification": issue.classification, "side_effect_class": issue.side_effect_class})
        else:
            issue.action = "rerun_queue_failed"
    except Exception as exc:
        issue.action = "rerun_queue_exception"
        issue.command_output = redact(str(exc))[:1200]
    return issue


def should_alert(issue: Issue, state: dict[str, Any], policy: dict[str, Any], force: bool, dry_run: bool) -> bool:
    if force or dry_run:
        return True
    incident = state.setdefault("incidents", {}).setdefault(issue.incident_key, {})
    if issue.alert_once:
        # A yellow threshold report is not a delivered CAP_RISK escalation.
        # Track RED separately from last-seen severity (which can change on
        # deduped ticks); recovery still uses the producer's warning episode.
        if issue.classification == 'n8n_execution_budget_70_percent' and issue.severity == 'red':
            return not incident.get('red_alert_at')
        return not incident.get('last_alert_at')
    first = incident.get("first_seen_at")
    if not first:
        return True
    if issue.action.startswith("queued") or issue.action.startswith("rerun_queue"):
        return True
    last_alert = parse_dt(incident.get("last_alert_at"))
    interval = int(
        issue.alert_min_interval_minutes
        if issue.alert_min_interval_minutes is not None
        else policy.get("defaults", {}).get("alert_min_interval_minutes", 360)
    )
    return not last_alert or utc_now() - last_alert > timedelta(minutes=interval)


def update_incident_state(issue: Issue, state: dict[str, Any], alerted: bool) -> None:
    inc = state.setdefault("incidents", {}).setdefault(issue.incident_key, {})
    now_s = iso()
    inc.setdefault("first_seen_at", now_s)
    inc["last_seen_at"] = now_s
    inc["job_id"] = issue.job_id
    inc["job_name"] = issue.job_name
    inc["classification"] = issue.classification
    inc["severity"] = issue.severity
    inc["side_effect_class"] = issue.side_effect_class
    inc["last_action"] = issue.action
    inc["operator_fix"] = issue.operator_fix
    if alerted:
        inc["last_alert_at"] = now_s
        if issue.classification == 'n8n_execution_budget_70_percent' and issue.severity == 'red':
            inc['red_alert_at'] = now_s


def raise_red_alert_for_issue(issue: Issue, *, live: bool = True) -> dict[str, Any]:
    is_enzyme = 'enzyme' in issue.job_name.lower()
    source = 'Enzyme' if is_enzyme else 'Cron Health'
    if is_enzyme:
        impact = 'vault reindex needs attention'
        what = "Enzyme's scheduled vault reindex failed, so search and recall may be stale."
    else:
        impact = 'scheduled job needs attention'
        what = 'A scheduled job failed, so its work may be stale until the issue is reviewed.'
    action = (
        'Confirm the queued safe rerun completes; inspect the report if it fails again.'
        if issue.action == 'queued_safe_rerun'
        else 'Inspect the failed job and verify side effects before any reset or rerun.'
    )
    where = issue.output_paths[0] if issue.output_paths else f'scheduled-job-health report · job {issue.job_id}'
    alert = {
        'severity': 'RED', 'source': source, 'impact': impact, 'what': what,
        'action': action, 'where': where,
        'evidence_codes': [issue.classification, issue.reason],
        'url': None, 'url_title': None,
        'dedup_key': f'{source}:{issue.incident_key or issue.job_id}',
        'emergency': False, 'recovery': False,
    }
    return red_alert_hook.raise_alert(alert, live=live)


def render_report(issues: list[Issue], dry_run: bool, report_path: Path | None) -> str:
    queued = [i for i in issues if i.action == "queued_safe_rerun"]
    blockers = [i for i in issues if i.action != "queued_safe_rerun"]
    if blockers:
        head = fmt_title('warn', 'Cron Health', f'{len(blockers)} job(s) need manual attention')
    else:
        head = fmt_title('infra', 'Cron Health', f'{len(queued)} safe rerun(s) queued')
    if dry_run:
        head += ' (dry run)'
    parts = [
        head,
        fmt_section('Decisions', fmt_kvline(
            ('safe reruns queued', len(queued)),
            ('manual attention', len(blockers)),
            ('mode', 'dry run' if dry_run else 'live watchdog'),
        )),
    ]
    for idx, issue in enumerate(issues[:10], 1):
        parts.append(fmt_section(
            f'Issue {idx} — {issue.job_name}',
            fmt_kvline(('severity', issue.severity), ('class', issue.classification), ('action', issue.action)),
            f'fix: {redact(issue.operator_fix)}',
        ))
    if len(issues) > 10:
        parts.append(fmt_kv('more issues in full report', len(issues) - 10))
    parts.append(fmt_meaning('inspect the manual items — auto-rerun items need nothing' if blockers else 'nothing right now — reruns queued automatically'))
    if report_path:
        parts.append(fmt_subtext(f'full report: {report_path}'))
    return fmt_render(*parts) + '\n'



def write_full_report(issues: list[Issue], dry_run: bool) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    root = REPORT_BASE / stamp
    root.mkdir(parents=True, exist_ok=True)
    md = root / "findings.md"
    data = {
        "generated_at_utc": iso(),
        "dry_run": dry_run,
        "issue_count": len(issues),
        "issues": [issue.__dict__ for issue in issues],
    }
    (root / "findings.json").write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    body = [f"# Scheduled Job Health Manager — {'dry run' if dry_run else 'alert/action'}", "", f"Generated: `{iso()}`", "", f"Issue count: `{len(issues)}`", ""]
    body.append(render_report(issues, dry_run=dry_run, report_path=None))
    md.write_text("\n".join(body), encoding="utf-8")
    return md


def os_cron_log_issues(policy: dict[str, Any]) -> list[Issue]:
    """Best-effort OS cron health: scan recent redirected logs for hard errors.

    This is report-only. OS cron commands are not rerun automatically because command
    idempotency and side effects are not reliably knowable from crontab text.
    """
    defaults = policy.get("defaults", {}) if isinstance(policy, dict) else {}
    recent_hours = int(defaults.get("recent_output_scan_hours", 12))
    max_bytes = int(defaults.get("output_scan_bytes", 20000))
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return [Issue("os-cron", "OS crontab", "system crontab", "unknown_review_before_mutation", "os_cron_inspection_failed", "yellow", "could_not_read_crontab", redact(str(exc)), "Run `crontab -l` manually and inspect user cron setup.", False, False)]
    if proc.returncode != 0:
        txt = (proc.stdout or "") + (proc.stderr or "")
        if "no crontab" in txt.lower():
            return []
        return [Issue("os-cron", "OS crontab", "system crontab", "unknown_review_before_mutation", "os_cron_inspection_failed", "yellow", "crontab_l_failed", redact(txt[:300]), "Run `crontab -l` manually and inspect user cron setup.", False, False)]
    issues: list[Issue] = []
    now_ts = utc_now().timestamp()
    hard_rx = re.compile(
        r"(?m)^STATUS:\\s*ERROR\\b|Traceback \\(most recent call last\\)|Unhandled exception|"
        r"RuntimeError:|ValueError:|ModuleNotFoundError|ImportError:|PermissionError|"
        r"Errno 13|sh: .*: not found|failed status=\\d+|Client Error:|HTTPError",
        re.I,
    )
    for i, line in enumerate(proc.stdout.splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split()
        if len(parts) < 6:
            continue
        schedule = " ".join(parts[:5])
        command = " ".join(parts[5:])
        receipt_ok = False
        for rm in re.finditer(r"--receipt\s+([^\s]+)", command):
            rp = Path(os.path.expanduser(rm.group(1).strip().strip('"\'')))
            if not rp.is_absolute():
                rp = BUSINESS / rp
            if rp.exists() and rp.is_file():
                try:
                    rdata = json.loads(rp.read_text(encoding="utf-8"))
                    completed = parse_dt(rdata.get("completed_at_utc"))
                    if not rdata.get("error") and completed and utc_now() - completed <= timedelta(hours=recent_hours):
                        receipt_ok = True
                        break
                except Exception:
                    pass
        if receipt_ok:
            continue
        paths = []
        for m in re.finditer(r"(?:>>|>)\s*([^\s]+)", command):
            candidate = m.group(1).strip().strip('"\'')
            if candidate in {"/dev/null", "2>&1"}:
                continue
            p = Path(os.path.expanduser(candidate))
            if not p.is_absolute():
                p = BUSINESS / p
            if p.exists() and p.is_file():
                paths.append(p)
        for p in paths:
            try:
                age = (now_ts - p.stat().st_mtime) / 3600
                if age > recent_hours:
                    continue
                text = p.read_bytes()[-max_bytes:].decode("utf-8", errors="replace")
                scan_text = text
                status_matches = list(re.finditer(r"(?m)^STATUS:\s*([A-Z0-9_ -]+)\s*$", text))
                if status_matches:
                    latest_status = status_matches[-1].group(1).strip().upper()
                    scan_text = text[status_matches[-1].start():]
                    if latest_status in {"OK", "NO_TASKS", "NO_NEW_BOOKMARKS", "NO_CHANGE", "NO_ACTION", "SKIP", "SKIPPED"}:
                        continue
                m = hard_rx.search(scan_text)
                if not m:
                    continue
                issues.append(Issue(
                    job_id=f"os-cron-line-{i}",
                    job_name=f"OS cron line {i}",
                    schedule=schedule,
                    side_effect_class="unknown_review_before_mutation",
                    classification="os_cron_recent_log_error",
                    severity="red",
                    reason="recent_redirected_log_contains_error",
                    evidence=redact(m.group(0))[:240],
                    operator_fix=f"Inspect `{p}` and the crontab command. This manager does not auto-rerun OS cron commands because side effects are unknown.",
                    can_auto_fix=False,
                    safe_to_rerun=False,
                    output_paths=[str(p)],
                ))
            except Exception as exc:
                issues.append(Issue(f"os-cron-line-{i}", f"OS cron line {i}", schedule, "unknown_review_before_mutation", "os_cron_log_read_failed", "yellow", "could_not_read_log", redact(str(exc)), f"Inspect log path `{p}` permissions/existence.", False, False, output_paths=[str(p)]))
    return issues


def systemd_failed_issues(policy: dict[str, Any]) -> list[Issue]:
    """Report failed user systemd units/timers. No automatic restarts by default."""
    try:
        proc = subprocess.run(["systemctl", "--user", "--failed", "--plain", "--no-pager"], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return [Issue("systemd-user", "systemd --user", "systemd", "unknown_review_before_mutation", "systemd_inspection_failed", "yellow", "systemctl_failed", redact(str(exc)), "Run `systemctl --user --failed` manually and inspect user service manager.", False, False)]
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "0 loaded units listed" in text.lower() or "0 loaded units" in text.lower():
        return []
    issues: list[Issue] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("UNIT ") or line.startswith("LOAD ") or line.startswith("●"):
            continue
        m = re.search(r"([A-Za-z0-9_.@:-]+\.(?:service|timer))\b", line)
        if not m:
            continue
        unit = m.group(1)
        issues.append(Issue(
            job_id=f"systemd-user-{unit}",
            job_name=f"systemd user unit {unit}",
            schedule="systemd --user failed unit",
            side_effect_class="unknown_review_before_mutation",
            classification="systemd_user_unit_failed",
            severity="red",
            reason="systemctl_user_failed",
            evidence=redact(line[:300]),
            operator_fix=f"Inspect `systemctl --user status {unit}` and `journalctl --user -u {unit}`. Reset/restart only after verifying side effects/idempotency.",
            can_auto_fix=False,
            safe_to_rerun=False,
        ))
    if proc.returncode not in (0, 1) and not issues:
        issues.append(Issue("systemd-user", "systemd --user", "systemd", "unknown_review_before_mutation", "systemd_inspection_failed", "yellow", "systemctl_returned_nonzero", redact(text[:300]), "Run `systemctl --user --failed` manually and inspect user service manager.", False, False))
    return issues


def load_nested_mission_receipts(spec: dict[str, Any]) -> dict[str, Any]:
    """Load exact-mission receipts using receipt timestamps, never mtimes."""
    root = Path(str(spec.get("receipt_root") or ""))
    mission_id = str(spec.get("mission_id") or "")
    if not root.is_dir() or not mission_id:
        return {"root": root, "paths": [], "receipts": [], "invalid": [], "newest_path": None}
    paths = sorted(root.glob(f"*-{mission_id}.json"), reverse=True)
    receipts: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            started = parse_dt(data.get("started_at_utc"))
            if str(data.get("mission_id") or "") != mission_id or started is None:
                raise ValueError("mission_id_or_started_at_invalid")
            receipts.append({"path": path, "started": started, "data": data})
        except Exception as exc:
            invalid.append({"path": str(path), "error": type(exc).__name__})
    receipts.sort(key=lambda item: item["started"], reverse=True)
    return {
        "root": root,
        "paths": paths,
        "receipts": receipts,
        "invalid": invalid,
        "newest_path": paths[0] if paths else None,
    }


def is_guard_blocked_receipt(receipt: dict[str, Any]) -> bool:
    return (
        str(receipt.get("status") or "").lower() == "blocked"
        and receipt.get("runner_returncode") == 2
        and receipt.get("run_dir") is None
    )


def nested_systemd_mission_issues(spec: dict[str, Any], now: datetime) -> list[Issue]:
    """Evaluate liveness behind systemd carriers with sanctioned exits."""
    unit = str(spec.get("unit") or "unknown.service")
    mission_id = str(spec.get("mission_id") or "unknown-mission")
    loaded = load_nested_mission_receipts(spec)
    root = loaded["root"]
    reminder = int(spec.get("alert_min_interval_minutes") or 1440)
    common = {
        "job_id": f"systemd-user-{unit}",
        "job_name": f"systemd user unit {unit}",
        "schedule": str(spec.get("schedule") or "nested systemd mission"),
        "side_effect_class": "local_repo_update_with_runtime_restart",
        "severity": str(spec.get("severity") or "yellow"),
        "can_auto_fix": False,
        "safe_to_rerun": False,
        "alert_min_interval_minutes": reminder,
    }

    def issue(classification: str, reason: str, evidence: str, fix: str, path: Path | None = None) -> Issue:
        return Issue(
            classification=classification,
            reason=reason,
            evidence=redact(evidence),
            operator_fix=fix,
            output_paths=[str(path)] if path else [],
            **common,
        )

    if not root.is_dir():
        return [issue(
            "nested_mission_receipt_root_missing",
            "receipt_root_missing",
            f"mission={mission_id}; receipt_root_missing",
            "Repair the receipt publisher/path before trusting the carrier unit; never blind-rerun this update/restart mission.",
        )]
    if not loaded["paths"]:
        return [issue(
            "nested_mission_receipt_missing",
            "no_exact_mission_receipts",
            f"mission={mission_id}; no_exact_receipts",
            "Inspect the timer/service and deterministic receipt publisher; never blind-rerun this update/restart mission.",
        )]
    newest_path = loaded["newest_path"]
    invalid_paths = {row["path"] for row in loaded["invalid"]}
    if newest_path and str(newest_path) in invalid_paths:
        return [issue(
            "nested_mission_receipt_invalid",
            "newest_atomic_receipt_invalid",
            f"mission={mission_id}; newest_receipt_invalid",
            "Repair the newest atomically published receipt before trusting systemd success.",
            newest_path,
        )]
    receipts = loaded["receipts"]
    if not receipts:
        return [issue(
            "nested_mission_receipt_invalid",
            "no_valid_exact_mission_receipts",
            f"mission={mission_id}; no_valid_receipts",
            "Repair the deterministic receipt schema before trusting systemd success.",
            newest_path,
        )]

    latest = receipts[0]
    data = latest["data"]
    path = latest["path"]
    age = now - latest["started"]
    status = str(data.get("status") or "").lower()
    running_timeout = timedelta(minutes=int(spec.get("running_timeout_minutes") or 555))
    terminal_stale = timedelta(hours=float(spec.get("terminal_stale_hours") or 36))

    if status == "running":
        if age > running_timeout:
            return [issue(
                "nested_mission_stuck",
                "running_receipt_exceeded_timeout",
                f"mission={mission_id}; running_timeout_exceeded",
                "Inspect the active service/process and exact run log; do not start a second updater.",
                path,
            )]
        return []
    if age > terminal_stale:
        return [issue(
            "nested_mission_receipt_stale",
            "terminal_receipt_stale",
            f"mission={mission_id}; terminal_receipt_stale",
            "Inspect timer/service freshness and the latest mission receipt before any manual run.",
            path,
        )]
    if status == "failed":
        return [issue(
            "nested_mission_failed",
            "latest_nested_mission_failed",
            f"mission={mission_id}; latest_status=failed",
            "Inspect the exact nested run and repair its deterministic failure before any replay.",
            path,
        )]
    if status not in {"completed", "blocked"}:
        return [issue(
            "nested_mission_receipt_invalid",
            "unknown_terminal_status",
            f"mission={mission_id}; unknown_terminal_status",
            "Repair the receipt status contract before trusting systemd success.",
            path,
        )]

    threshold = max(1, int(spec.get("consecutive_guard_block_threshold") or 2))
    streak = 0
    for row in receipts:
        if is_guard_blocked_receipt(row["data"]):
            streak += 1
        else:
            break
    if streak >= threshold:
        return [issue(
            "nested_mission_repeated_guard_block",
            "consecutive_guard_block_threshold_reached",
            f"mission={mission_id}; repeated_guard_block",
            "Resolve the exact fail-closed update gate in an isolated worktree and write the bound repair receipt; do not blind-rerun or mutate the live checkout.",
            path,
        )]
    return []


def primary_issue_per_job(issues: list[Issue]) -> list[Issue]:
    """Avoid Discord spam: keep one primary actionable issue per job."""
    def score(issue: Issue) -> tuple[int, int, int]:
        severity_score = {"red": 3, "yellow": 2, "green": 1}.get(issue.severity, 1)
        generic_penalty = 0 if issue.classification.startswith("generic_") else 1
        known_specific = 1 if issue.reason not in {"last_status=error", "last_status=failed"} else 0
        return (severity_score, generic_penalty, known_specific)
    best: dict[str, Issue] = {}
    for issue in issues:
        cur = best.get(issue.job_id)
        if cur is None or score(issue) > score(cur):
            best[issue.job_id] = issue
    return list(best.values())



def collect_daily_source_drift(state, now):
    """First existing health tick after 08:00 PT; no new schedule or agent."""
    if not scheduled_source_drift.daily_due(state, now):
        return None
    try:
        result = scheduled_source_drift.live_audit()
    except Exception:
        result = {'status': 'UNKNOWN', 'counts': {'script_drift': 0,
                  'scheduler_drift': 0, 'payload_drift': 0, 'unknown': 1},
                  'findings': [{'kind': 'unknown', 'reason': 'drift_check_failed'}]}
    state['last_source_drift_day'] = now.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
    state['last_source_drift'] = result
    return result


def run(dry_run: bool, force_report: bool, verbose: bool) -> int:
    now = utc_now()
    policy = load_yaml(POLICY_PATH, {})
    side_policy = load_yaml(SIDE_EFFECT_PATH, {})
    regmap = build_registry_map()
    state = load_json(STATE_PATH, {"version": 1, "incidents": {}})
    source_drift = collect_daily_source_drift(state, now)
    jobs = job_list()
    defaults = policy.get("defaults", {}) if isinstance(policy, dict) else {}
    max_files = int(defaults.get("max_output_files_per_job", 3))
    max_bytes = int(defaults.get("output_scan_bytes", 20000))
    recent_hours = int(defaults.get("recent_output_scan_hours", 12))
    issues: list[Issue] = []
    for job in jobs:
        if not bool(job.get("enabled", True)):
            continue
        if should_exclude(job, policy):
            continue
        jid = str(job.get("id") or job.get("job_id") or "")
        if not jid:
            continue
        side_class = side_effect_class(job, regmap)
        status = str(job.get("last_status") or "").lower()
        delivery_error = str(job.get("last_delivery_error") or "").strip()
        include_old_output = bool(status and status not in {"ok", "success", "none", "null"}) or bool(delivery_error)
        scan_files = output_scan_limit(status, delivery_error, max_files)
        text, paths = latest_output_text(jid, max_files=scan_files, max_bytes=max_bytes, recent_hours=recent_hours, include_old=include_old_output)
        combined = "\n".join([
            redact(str(job.get("last_status") or "")),
            redact(str(job.get("last_delivery_error") or "")),
            text,
        ])
        sv = schedule_validation_issue(job, side_class)
        if sv:
            issues.append(sv)
        issues.extend(generic_status_issues(job, side_class, now, policy, paths))
        issues.extend(pattern_issues(job, side_class, combined, paths, policy))
    watch = policy.get("watch", {}) if isinstance(policy, dict) else {}
    if watch.get("os_cron"):
        issues.extend(os_cron_log_issues(policy))
    for spec in watch.get("nested_systemd_missions", []) or []:
        if isinstance(spec, dict):
            issues.extend(nested_systemd_mission_issues(spec, now))
    if watch.get("systemd_user_services") or watch.get("systemd_user_timers"):
        issues.extend(systemd_failed_issues(policy))
    heartbeat_audit = monitor_heartbeats.audit(now=now)
    for fault in heartbeat_audit.get("issues", []):
        monitor = str(fault.get("monitor") or "unknown-monitor")
        severity = "red" if str(fault.get("severity")).upper() == "RED" else "yellow"
        issues.append(Issue(
            job_id=f"monitor-heartbeat-{monitor}",
            job_name=f"Monitor heartbeat: {monitor}",
            schedule=f"every {fault.get('cadence_s')}s",
            side_effect_class="read_report_monitor",
            classification="monitor_heartbeat_fault",
            severity=severity,
            reason=str(fault.get("reason") or "heartbeat_fault"),
            evidence=f"age_s={fault.get('age_s')}; cadence_s={fault.get('cadence_s')}; upstream/auth proof required",
            operator_fix="Repair the named monitor's upstream/auth path and verify a fresh proof heartbeat. Do not infer health from exit code alone.",
            can_auto_fix=False,
            safe_to_rerun=False,
        ))
    budget = n8n_execution_budget.health_check(now=now, dry_run=dry_run)
    if budget.get('daily_line'):
        print(budget['daily_line'])
    for fault in budget.get('issues', [budget['issue']] if budget.get('issue') else []):
        issues.append(Issue(
            job_id=fault.get('job_id', 'n8n-execution-budget'), job_name='n8n monthly execution budget / invariant guard',
            schedule='hourly cached check / daily usage line',
            side_effect_class='read_report_monitor',
            classification=fault['classification'], severity=fault['severity'],
            reason=fault['reason'], evidence=fault['evidence'],
            operator_fix=fault['operator_fix'], can_auto_fix=False, safe_to_rerun=False,
            incident_key=fault.get('incident_key', ''), alert_once=fault.get('alert_once', False),
        ))
    state['last_n8n_execution_budget'] = budget['snapshot']
    issues = primary_issue_per_job(issues)
    # De-dupe by job + primary classification fingerprint.
    unique: dict[str, Issue] = {}
    fill_incident_keys(issues)
    for issue in issues:
        unique.setdefault(issue.incident_key, issue)
    issues = list(unique.values())
    for issue in issues:
        decide_and_maybe_rerun(issue, state, side_policy, policy, dry_run=dry_run)
    alertable = []
    red_alert_receipts = []
    for issue in issues:
        alert = should_alert(issue, state, policy, force=force_report, dry_run=dry_run)
        update_incident_state(issue, state, alerted=alert)
        if alert:
            alertable.append(issue)
            if issue.severity == "red" and not dry_run:
                red_alert_receipts.append(raise_red_alert_for_issue(issue, live=True))
    state["last_run_at"] = iso(now)
    state["last_issue_count"] = len(issues)
    state["last_alert_count"] = len(alertable)
    state["last_job_count"] = len(jobs)
    state["last_monitor_heartbeat_audit"] = {
        "status": heartbeat_audit.get("status"),
        "registered_monitors": heartbeat_audit.get("registered_monitors"),
        "issue_count": heartbeat_audit.get("issue_count"),
        "red_count": heartbeat_audit.get("red_count"),
        "yellow_count": heartbeat_audit.get("yellow_count"),
    }
    if not dry_run:
        save_json(STATE_PATH, state)
        append_ledger({"ts": iso(now), "event": "scheduled_job_health_check", "job_count": len(jobs), "issue_count": len(issues), "alert_count": len(alertable), "red_alert_count": len(red_alert_receipts), "dry_run": dry_run})
    if dry_run or force_report or alertable or source_drift is not None:
        report_path = write_full_report(alertable if alertable else issues, dry_run=dry_run)
        msg = render_report(alertable if alertable else issues, dry_run=dry_run, report_path=report_path)
        if source_drift is not None:
            # Drift is daily report-only evidence, never a rerun or immediate RED alert.
            save_json(report_path.parent / 'source-drift.json', source_drift)
            msg += '\n\n' + scheduled_source_drift.render_summary(source_drift)
        print(msg)
    elif verbose:
        print(json.dumps({"status": "healthy_or_deduped", "job_count": len(jobs), "issue_count": len(issues), "alert_count": len(alertable)}, indent=2))
    return 0


def red_alert_disarmed_test() -> int:
    DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
    receipt = red_alert_hook.raise_alert({
        'severity': 'RED', 'source': 'Cron Health', 'impact': 'offline alert fixture queued',
        'what': 'An offline scheduled-job alert fixture was rendered without requesting delivery.',
        'action': 'None — this is an offline fixture.',
        'where': 'scheduled_job_health_manager.py · fixture disarmed-local-test',
        'evidence_codes': [], 'url': None, 'url_title': None,
        'dedup_key': f'{SCRIPT_NAME}:disarmed-local-test',
        'emergency': False, 'recovery': False,
    }, live=False)
    out = DELIVERY_DIR / "s2-scheduled-job-health-red-alert-disarmed-test.json"
    save_json(out, {"status": "pass", "live_send": False, "hook_receipt": receipt, "secret_printed": False})
    print(json.dumps({"status": "pass", "receipt": str(out), "live_send": False, "secret_printed": False}, indent=2))
    return 0


def red_alert_real_branch_fixture_test() -> int:
    DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
    issue = Issue(
        job_id="fixture-red-job", job_name="Fixture RED job", schedule="fixture",
        side_effect_class="read_report_monitor", classification="fixture_real_branch_red",
        severity="red", reason="fixture_red_issue", evidence="fixture evidence",
        operator_fix="fixture operator fix", can_auto_fix=False, safe_to_rerun=False,
        action="alert_no_rerun", incident_key="fixture-red-job:fixture_real_branch_red:s2",
    )
    receipt = raise_red_alert_for_issue(issue, live=False)
    out = DELIVERY_DIR / "s2-scheduled-job-health-real-branch-fixture.json"
    save_json(out, {"status": "pass", "real_branch": "scheduled_job_health_red_issue_alertable", "live_send": False, "hook_receipt": receipt, "secret_printed": False})
    print(json.dumps({"status": "pass", "receipt": str(out), "live_send": False, "secret_printed": False}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Investigate and report without queueing reruns")
    parser.add_argument("--force-report", action="store_true", help="Print report even if incidents are deduped")
    parser.add_argument("--verbose", action="store_true", help="Print healthy/deduped JSON summary")
    parser.add_argument("--red-alert-test", action="store_true", help="DISARMED local hook test; queues only, never sends Pushover")
    parser.add_argument("--s2-fixture-red-branch", action="store_true", help="Exercise the real RED issue hook path with live=False")
    args = parser.parse_args()
    if args.red_alert_test:
        return red_alert_disarmed_test()
    if args.s2_fixture_red_branch:
        return red_alert_real_branch_fixture_test()
    return run(dry_run=args.dry_run, force_report=args.force_report, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
