#!/usr/bin/env python3
"""One Sunday SEO control job: learn, measure, reorient, report.

The existing publishing/indexation rails are deliberately not rebuilt. The
original full-weekly path retains its learning, collection, policy and Discord
behavior. Explicit --gsc-only uses only the maintained GSC sitewide collector
under the same controller lock, and never claims full-weekly acceptance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
if str(BUSINESS) not in sys.path:
    sys.path.insert(0, str(BUSINESS))
LEARNING_SCRIPT = Path("/home/ubuntu/.hermes/scripts/glp_seo_weekly_learning.sh")
REPORT_SCRIPT = BUSINESS / "scripts/glp_weekly_seo_ranking_report.py"
ADJUST_SCRIPT = BUSINESS / "scripts/glp_seo_targeting_adjust.py"
REPORT_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-ranking-report"
LEARNING_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-learning"
CONTROL_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-control"
POLICY_PATH = BUSINESS / "reports/glp-clinic-scout/seo-machine/targeting-policy/current-targeting-policy.json"
LOCK_PATH = Path("/home/ubuntu/.hermes/state/glp-seo-weekly-control.lock")
PT = ZoneInfo("America/Los_Angeles")
REPORT_MAX_AGE_HOURS = 3.0


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def validate_live_report(
    report: dict[str, Any], report_path: Path, report_date: dt.date,
    *, now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Reject stale, unbound, or effectful evidence before changing strategy."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    captured = parse_utc(str(report.get("captured_at_utc") or ""))
    age_hours = (now - captured).total_seconds() / 3600.0
    evidence = report.get("evidence") or []
    evidence_verified = 0
    mutable_evidence_captured = 0
    mutable_paths = {
        BUSINESS / "reports/glp-clinic-scout/seo-machine/published-route-ledger.json",
    }
    for item in evidence:
        path = Path(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        if not path.is_absolute() or not path.is_file() or not expected:
            raise RuntimeError(f"weekly_report_evidence_unverified:{path}")
        if path in mutable_paths:
            # The publisher may append after report capture; the hash stored in the
            # immutable report remains provenance and must not freeze publishing.
            mutable_evidence_captured += 1
        elif sha256(path) == expected:
            evidence_verified += 1
        else:
            raise RuntimeError(f"weekly_report_evidence_unverified:{path}")
    effects = report.get("effects") or {}
    dataforseo_captured = parse_utc(
        str((report.get("dataforseo_context") or {}).get("captured_at_utc") or "")
    )
    dataforseo_age_hours = (now - dataforseo_captured).total_seconds() / 3600.0
    checks = {
        "schema": report.get("schema") == "glp.weekly-seo-ranking-report.v1",
        "report_date": report.get("report_date") == report_date.isoformat(),
        "captured_fresh": -0.1 <= age_hours <= REPORT_MAX_AGE_HOURS,
        "gsc_domain_property": (report.get("google_indexation") or {}).get("property")
        == "sc-domain:glpclinicscout.com",
        "bing_authority": (report.get("bing") or {}).get("source")
        == "Cogny Bing Webmaster Tools",
        "dataforseo_fresh": -0.1 <= dataforseo_age_hours <= REPORT_MAX_AGE_HOURS,
        "headline_present": all(
            key in (report.get("headline") or {})
            for key in ("published_article_urls", "google_indexed",
                        "google_clicks_latest_complete_week_indexed_pages")
        ),
        "no_site_or_sheet_or_tracking_effect": all(
            int(effects.get(key) or 0) == 0
            for key in ("site_deploys", "sheet_writes", "tracking_links_opened_or_clicked")
        ),
        "evidence_bound": bool(evidence)
        and evidence_verified + mutable_evidence_captured == len(evidence),
        "report_path_absolute": report_path.is_absolute(),
    }
    if not all(checks.values()):
        failed = sorted(key for key, value in checks.items() if not value)
        raise RuntimeError("weekly_report_validation_failed:" + ",".join(failed))
    return {
        "captured_at_utc": report["captured_at_utc"],
        "age_hours": round(age_hours, 4),
        "max_age_hours": REPORT_MAX_AGE_HOURS,
        "immutable_evidence_files_verified": evidence_verified,
        "mutable_evidence_hashes_captured": mutable_evidence_captured,
        "checks": checks,
    }


def completed_receipt(report_date: dt.date) -> dict[str, Any] | None:
    """Return a verified week receipt; existing-but-changed state fails closed."""
    path = CONTROL_ROOT / report_date.isoformat() / "receipt.json"
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("status") not in {"PASS", "PASS_DEGRADED"}:
            raise RuntimeError("idempotency_conflict:terminal_receipt_status")
        expected = str(receipt.get("receipt_sha256") or "")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        actual = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not expected or actual != expected:
            raise RuntimeError("idempotency_conflict:terminal_receipt_hash")
        for source_name, source in (receipt.get("sources") or {}).items():
            if not isinstance(source, dict) or "path" not in source:
                continue
            source_path = Path(str(source.get("path") or ""))
            if (not source_path.is_absolute() or not source_path.is_file()
                    or sha256(source_path) != str(source.get("sha256") or "")):
                raise RuntimeError(f"idempotency_conflict:source:{source_name}")
        discord_path = REPORT_ROOT / f"{report_date.isoformat()}-discord-receipt.json"
        discord = json.loads(discord_path.read_text(encoding="utf-8"))
        if discord.get("status") != "PASS" or discord.get("exact_readback") is not True:
            raise RuntimeError("idempotency_conflict:discord_receipt")
    except RuntimeError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"idempotency_conflict:unreadable:{type(exc).__name__}") from exc
    return {**receipt, "receipt": str(path), "idempotent_replay": True}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module_load_failed:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_command(argv: list[str], timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=BUSINESS,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
    }


def latest_json_line(value: str) -> dict[str, Any]:
    for line in reversed(value.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("command_stdout_json_missing")


def learning_run(before: set[str]) -> dict[str, Any]:
    execution = run_command([str(LEARNING_SCRIPT)], timeout=1500)
    current = {str(path) for path in LEARNING_ROOT.iterdir() if path.is_dir()} if LEARNING_ROOT.is_dir() else set()
    created = sorted(current - before)
    run_dir = Path(created[-1]) if created else None
    required = ["digest.json", "topic-queue.json", "improvement-loops.json",
                "actions/action-manifest.json"]
    missing = ([name for name in required if not (run_dir / name).is_file()]
               if run_dir else required)
    blocked_text = "run blocked before analysis" in execution["stdout"].lower()
    status = (
        "PASS_WEEKLY_LEARNING" if execution["returncode"] == 0 and run_dir and not missing and not blocked_text
        else "BLOCKED_WEEKLY_LEARNING"
    )
    manifest: dict[str, Any] = {}
    digest: dict[str, Any] = {}
    if run_dir and (run_dir / "actions/action-manifest.json").is_file():
        manifest = json.loads((run_dir / "actions/action-manifest.json").read_text(encoding="utf-8"))
    if run_dir and (run_dir / "digest.json").is_file():
        digest = json.loads((run_dir / "digest.json").read_text(encoding="utf-8"))
    return {
        "status": status,
        "returncode": execution["returncode"],
        "run_dir": str(run_dir) if run_dir else None,
        "missing": missing,
        "action_counts": manifest.get("counts") or {},
        "cadence_decision": (digest.get("cadence_decision") or {}).get("decision"),
        "stdout_sha256": hashlib.sha256(execution["stdout"].encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(execution["stderr"].encode()).hexdigest(),
        "effects": (manifest.get("effects") or {
            "page_mutations": 0, "publishes": 0, "deployments": 0,
        }),
    }


def render_message(reporter, report: dict[str, Any], policy: dict[str, Any], learning: dict[str, Any]) -> str:
    base = reporter.render_discord(report).rstrip()
    lines = base.splitlines()
    if lines and lines[-1].startswith("-# Full report:"):
        lines.pop()
    weights = policy["class_weights"]
    actions = learning.get("action_counts") or {}
    message = "\n".join(lines) + "\n\n" + (
        "**Strategy applied:** provider reviews **{review}%** (was {prior}%), commercial comparison "
        "**{comparison}%**, cost **{cost}%**, medication guides **{medication}%**, education "
        "**{education}%**. Existing onboarding review generation stays intact; no duplicate review pages.\n"
        "**Weekly adjuster:** learning **{learning_status}** · actions P1/P2/P3/P4/P5/P6 "
        "**{p1}/{p2}/{p3}/{p4}/{p5}/{p6}** · selector policy is fail-open.\n"
        "-# Report: {report_path} · policy: {policy_path}"
    ).format(
        review=weights["provider_review"],
        prior=policy["baseline_class_weights"]["provider_review"],
        comparison=weights["commercial_comparison"],
        cost=weights["cost_pricing"],
        medication=weights["medication_guide"],
        education=weights["education_condition"],
        learning_status=learning["status"].replace("_", " "),
        p1=actions.get("P1", 0), p2=actions.get("P2", 0), p3=actions.get("P3", 0),
        p4=actions.get("P4", 0), p5=actions.get("P5", 0), p6=actions.get("P6", 0),
        report_path=REPORT_ROOT / f"{report['report_date']}.json",
        policy_path=POLICY_PATH,
    )
    if len(message) > 1900:
        raise RuntimeError(f"discord_control_report_too_long:{len(message)}")
    return message


def write_json_once(path: Path, value: dict[str, Any]) -> None:
    """Commit a dated GSC artifact once; an interrupted write needs reconciliation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def gsc_review(aggregate: dict[str, Any], report_date: dt.date, reporter,
               *, now: dt.datetime) -> dict[str, Any]:
    """Review only the two independent property totals in persisted evidence."""
    if aggregate.get("schema") != "glp.weekly-gsc-sitewide-aggregate.v1":
        raise ValueError("gsc_aggregate_schema_invalid")
    window = reporter.report_windows(report_date)["gsc_current"]
    days = [(window[0] + dt.timedelta(days=n)).isoformat()
            for n in range((window[1] - window[0]).days + 1)]
    if len(days) != 7 or set(aggregate.get("scopes") or {}) != {
            "consumer_https_apex", "domain_including_partners"}:
        raise ValueError("gsc_aggregate_scopes_invalid")
    fields = ("clicks", "impressions", "ctr", "position")
    output = {}
    for label, property_uri in (("consumer_https_apex", "https://glpclinicscout.com/"),
                                ("domain_including_partners", "sc-domain:glpclinicscout.com")):
        item = aggregate["scopes"][label]
        ctx = item.get("context") or {}
        expected = {"property": property_uri, "start_date": days[0], "end_date": days[-1],
                    "window_days": 7, "search_type": "web", "data_state": "final",
                    "dimensions": [], "aggregation_type": "byProperty",
                    "timezone": "America/Los_Angeles"}
        if any(ctx.get(key) != value for key, value in expected.items()) or not ctx.get("denominator"):
            raise ValueError("gsc_aggregate_context_invalid:" + label)
        began, captured = (parse_utc(str(item.get(key) or "")) for key in
                           ("request_started_at_utc", "captured_at_utc"))
        if (not item["request_started_at_utc"].endswith(("Z", "+00:00"))
                or not item["captured_at_utc"].endswith(("Z", "+00:00"))
                or began > captured or captured > now + dt.timedelta(minutes=6)
                or (now - captured).total_seconds() > REPORT_MAX_AGE_HOURS * 3600):
            raise ValueError("gsc_capture_clock_invalid:" + label)
        rows = item.get("date_availability")
        if not isinstance(rows, list) or [row.get("date") for row in rows] != days:
            raise ValueError("gsc_date_coverage_invalid:" + label)
        def valid_metrics(metrics: Any, known: bool) -> bool:
            return (isinstance(metrics, dict) and set(metrics) == set(fields)
                    and all((type(metrics[key]) in (int, float) and math.isfinite(metrics[key])
                             and metrics[key] >= 0) if known else metrics[key] is None
                            for key in fields))
        missing = []
        for row in rows:
            state = row.get("state")
            if state not in ("SOURCE_REPORTED", "UNKNOWN") or not valid_metrics(
                    row.get("metrics"), state == "SOURCE_REPORTED"):
                raise ValueError("gsc_date_metrics_invalid:" + label)
            if state == "UNKNOWN":
                missing.append(row["date"])
        state = item.get("state")
        expected_state = ("UNKNOWN_DATE_COVERAGE" if missing else
                          "UNKNOWN_NO_AGGREGATE_ROW" if not valid_metrics(item.get("totals"), True)
                          else "SOURCE_REPORTED")
        if (state != expected_state or item.get("missing_dates") != missing
                or not valid_metrics(item.get("totals"), state == "SOURCE_REPORTED")):
            raise ValueError("gsc_totals_state_invalid:" + label)
        output[label] = {"property": property_uri, "denominator": ctx["denominator"],
                         "state": state, "totals": item["totals"],
                         "missing_dates": missing, "request_started_at_utc": item["request_started_at_utc"],
                         "captured_at_utc": item["captured_at_utc"]}
    return {"status": "GSC_ONLY_PARTIAL_REVIEW", "report_date": report_date.isoformat(),
            "window": f"{days[0]}..{days[-1]}", "window_holdback_days": 3,
            "scope": "GSC sitewide only; separate denominators, never added",
            "interpretation": "Final requested dates are not a global watermark; Search clicks are not users, conversions or revenue.",
            "scopes": output}


def gsc_only(report_date: dt.date) -> dict[str, Any]:
    """Explicit manual branch of the registered locked controller, not full weekly."""
    aggregate_path = REPORT_ROOT / f"{report_date.isoformat()}-gsc-sitewide-aggregate.json"
    receipt_path = CONTROL_ROOT / report_date.isoformat() / "gsc-only-receipt.json"
    full_report = REPORT_ROOT / f"{report_date.isoformat()}.json"
    full_receipt = receipt_path.with_name("receipt.json")
    controller_path = BUSINESS / "scripts/glp_weekly_seo_control.py"
    rail_path = BUSINESS / "ops/glp/seo_data_rails.py"
    pin_guard = load_module("weekly_gsc_pin_guard", BUSINESS / "scripts/glp_weekly_seo_analysis.py")
    pin_guard.verify_pins()
    reporter = load_module("glp_weekly_gsc_collector", REPORT_SCRIPT)
    if full_report.exists() or full_receipt.exists():
        raise RuntimeError("gsc_only_full_weekly_collision")
    if receipt_path.is_symlink() or receipt_path.parent.is_symlink():
        raise RuntimeError("gsc_only_symlink_conflict")
    if receipt_path.exists():
        receipt_raw = receipt_path.read_bytes()
        receipt = json.loads(receipt_raw)
        if receipt_raw != (json.dumps(receipt, indent=2, sort_keys=True,
                                      ensure_ascii=False) + "\n").encode():
            raise RuntimeError("gsc_only_receipt_bytes_drift")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (receipt.get("receipt_sha256") != digest or receipt.get("status") != "GSC_ONLY_PARTIAL_REVIEW"
                or receipt.get("trigger_identity") != "manual_explicit_gsc_only"
                or receipt.get("report_date") != report_date.isoformat()
                or receipt.get("aggregate", {}).get("path") != str(aggregate_path)
                or {key: value.get("path") for key, value in receipt.get("sources", {}).items()} != {
                    "producer": str(REPORT_SCRIPT), "consumer": str(controller_path),
                    "gsc_rail": str(rail_path)}):
            raise RuntimeError("gsc_only_receipt_conflict")
        for binding in (receipt["aggregate"], *receipt["sources"].values()):
            path = Path(binding["path"])
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or sha256(path) != binding["sha256"]
                    or ("bytes" in binding and path.stat().st_size != binding["bytes"])):
                raise RuntimeError("gsc_only_source_drift")
        aggregate = json.loads(aggregate_path.read_bytes())
        # Replay validates shape and original chronology, not present-day freshness.
        captured = max(parse_utc(row["captured_at_utc"]) for row in aggregate["scopes"].values())
        if receipt["review"] != gsc_review(aggregate, report_date, reporter, now=captured):
            raise RuntimeError("gsc_only_review_drift")
        return {**receipt, "receipt": str(receipt_path), "idempotent_replay": True}
    if aggregate_path.exists() or receipt_path.parent.exists():
        raise RuntimeError("gsc_only_incomplete_or_colliding_state")
    if report_date != dt.datetime.now(PT).date():
        raise ValueError("gsc_only_historical_collection_refused")
    sources = {"producer": {"path": str(REPORT_SCRIPT), "sha256": sha256(REPORT_SCRIPT)},
               "consumer": {"path": str(controller_path), "sha256": sha256(controller_path)},
               "gsc_rail": {"path": str(rail_path), "sha256": sha256(rail_path)}}
    aggregate = reporter.collect_sitewide_gsc(
        reporter.GoogleRail(), reporter.report_windows(report_date)["gsc_current"])
    if any(sha256(Path(binding["path"])) != binding["sha256"] for binding in sources.values()):
        raise RuntimeError("gsc_only_source_changed_during_collection")
    # Validate before writing; review will be recomputed from reopened bytes.
    gsc_review(aggregate, report_date, reporter, now=dt.datetime.now(dt.timezone.utc))
    write_json_once(aggregate_path, aggregate)
    raw = aggregate_path.read_bytes()
    binding = {"path": str(aggregate_path), "sha256": hashlib.sha256(raw).hexdigest(),
               "bytes": len(raw)}
    review = gsc_review(json.loads(raw), report_date, reporter, now=dt.datetime.now(dt.timezone.utc))
    receipt = {"schema": "glp.weekly-gsc-only-consumer.v1", "machine": "glp_weekly_seo_control",
               "status": "GSC_ONLY_PARTIAL_REVIEW", "trigger_identity": "manual_explicit_gsc_only",
               "report_date": report_date.isoformat(), "reviewed_at_utc": utc_now(),
               "aggregate": binding, "review": review, "sources": sources,
               "full_weekly_pass": False, "natural_weekly_run_observed": False}
    receipt["receipt_sha256"] = hashlib.sha256(json.dumps(receipt, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()
    write_json_once(receipt_path, receipt)
    return {**receipt, "receipt": str(receipt_path)}


def execute(report_date: dt.date, module_input: Path | None = None) -> dict[str, Any]:
    from ops.glp.seo_business_context import load_business_context
    business_context = load_business_context('weekly_control')
    started = utc_now()
    before = ({str(path) for path in LEARNING_ROOT.iterdir() if path.is_dir()}
              if LEARNING_ROOT.is_dir() else set())
    stage = load_module("weekly_source_stage", BUSINESS / "scripts/glp_weekly_seo_source_stage.py")
    module_receipt = stage.run(report_date, module_input=module_input, registered=True)
    module_manifest = Path(module_receipt["module_input"])
    if not module_manifest.is_file() or not module_receipt.get("receipt"):
        raise RuntimeError("weekly_source_manifest_unbound_reconcile_no_report")
    collection = json.loads((module_manifest.parent / 'COLLECTION-RESULT.json').read_text())
    if (collection['manifest']['path'] != str(module_manifest)
            or collection['manifest']['sha256'] != sha256(module_manifest)
            or json.loads(module_manifest.read_text()).get('run_id') != collection['run_id']):
        raise RuntimeError("weekly_source_manifest_binding_invalid")
    learning = learning_run(before)

    report_execution = run_command(
        [sys.executable, str(REPORT_SCRIPT), "--report-date", report_date.isoformat(),
         "--module-input", str(module_manifest)],
        timeout=1800,
    )
    if report_execution["returncode"] != 0:
        raise RuntimeError("weekly_ranking_report_failed")
    report_summary = latest_json_line(report_execution["stdout"])
    report_path = REPORT_ROOT / f"{report_date.isoformat()}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source_freshness = validate_live_report(report, report_path, report_date)

    adjust_execution = run_command(
        [sys.executable, str(ADJUST_SCRIPT), "--report", str(report_path)], timeout=120,
    )
    if adjust_execution["returncode"] != 0:
        raise RuntimeError("weekly_targeting_adjust_failed")
    adjust_summary = latest_json_line(adjust_execution["stdout"])
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    reporter = load_module("glp_weekly_reporter_control", REPORT_SCRIPT)
    message = render_message(reporter, report, policy, learning)
    message += "\nS3b source stage: " + module_receipt["status"] + " · build/release held."
    discord = reporter.post_discord_once(report_date, message)
    status = "PASS" if learning["status"].startswith("PASS") else "PASS_DEGRADED"
    if module_receipt and module_receipt["status"] not in ("PASS_ANALYSIS_ONLY", "PASS_WEEKLY_SOURCE_STAGED"):
        status = "PASS_DEGRADED"
    receipt = {
        "schema": "glp-seo-weekly-control-v1",
        "machine": "glp_weekly_seo_control",
        "status": status,
        "started_at_utc": started,
        "finished_at_utc": utc_now(),
        "report_date": report_date.isoformat(),
        "business_context": {k: v for k, v in business_context.items() if k != 'text'},
        "verdict": report["verdict"]["status"],
        "published": report["headline"]["published_article_urls"]["value"],
        "indexed": report["headline"]["google_indexed"]["value"],
        "clicks": report["headline"]["google_clicks_latest_complete_week_indexed_pages"]["value"],
        "page_family_hypothesis": report["page_family_analysis"]["hypothesis_test"],
        "targeting": adjust_summary,
        "weekly_learning": learning,
        "weekly_modules": module_receipt,
        "discord": discord,
        "sources": {
            **({"weekly_modules": {"path": module_receipt["receipt"], "sha256": module_receipt["sha256"]}}
               if module_receipt and module_receipt.get("receipt") else {}),
            "ranking_report": {"path": str(report_path), "sha256": sha256(report_path)},
            "targeting_policy": {"path": str(POLICY_PATH), "sha256": sha256(POLICY_PATH)},
            "weekly_learning": learning.get("run_dir"),
        },
        "source_freshness": source_freshness,
        "effects": {
            "site_deploys": 0,
            "sheet_writes": 0,
            "tracking_links_opened_or_clicked": 0,
            "selector_policy_writes": 1,
            "discord_posts_or_edits": 1,
            "dataforseo_actual_cost_usd": (report.get("effects") or {}).get(
                "dataforseo_actual_cost_usd"),
        },
        "skills_mcps_data_rails": {
            "skills": [
                "marketing/content-strategy", "dogfood/operator-review-reporting",
                "mcp/mcp-operations", "email/google-workspace-oauth-operations",
                "research/grounded-citations", "social-media/discord-rest-messaging",
                "dogfood/loopos-production-loops",
            ],
            "mcps": ["Cogny Bing Webmaster Tools", "n8n control plane"],
            "data_rails": [
                "GSC direct user-OAuth sc-domain property", "DataForSEO",
                "Cogny Bing Webmaster Tools", "GA4 direct user-OAuth", "PostHog",
                "published route ledger", "R2 final publication receipts",
            ],
        },
    }
    unsigned = dict(receipt)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    dated = CONTROL_ROOT / report_date.isoformat() / "receipt.json"
    latest = CONTROL_ROOT / "latest.json"
    write_json(dated, receipt)
    write_json(latest, receipt)
    return {**receipt, "receipt": str(dated)}


def resume_modules(report_date: dt.date, prior: dict[str, Any]) -> dict[str, Any]:
    """Finish verified weekly checkpoints, never replay learning or collection."""
    receipt_path = CONTROL_ROOT / report_date.isoformat() / 'receipt.json'
    stored = json.loads(receipt_path.read_text())
    if stored['receipt_sha256'] != prior['receipt_sha256']:
        raise RuntimeError('weekly_resume_prior_receipt_drift')
    archive = receipt_path.with_name('receipt-before-module-resume-' + stored['receipt_sha256'] + '.json')
    if archive.exists():
        if archive.read_bytes() != receipt_path.read_bytes():
            raise RuntimeError('weekly_resume_archive_drift')
    else:
        with archive.open('xb') as handle:
            handle.write(receipt_path.read_bytes())
    stage = load_module('weekly_source_stage_resume', BUSINESS / 'scripts/glp_weekly_seo_source_stage.py')
    module_receipt = stage.run(report_date, registered=True)
    result = {k: v for k, v in stored.items() if k != 'receipt_sha256'}
    result['weekly_modules'] = module_receipt
    result['status'] = ('PASS' if module_receipt['status'] == 'PASS_WEEKLY_SOURCE_STAGED'
                        and stored['weekly_learning']['status'] == 'PASS_WEEKLY_LEARNING'
                        else 'PASS_DEGRADED')
    result['finished_at_utc'] = utc_now()
    result['module_resume'] = {'prior_receipt_sha256': stored['receipt_sha256'],
                               'archive': str(archive), 'incremental_dataforseo_cost_usd': 0}
    result['sources']['weekly_modules'] = {'path': module_receipt['receipt'], 'sha256': module_receipt['sha256']}
    strategy_path = Path(module_receipt['strategy_note']).with_name('STRATEGY.json')
    strategy_copy = receipt_path.with_name('strategy.json')
    strategy_copy.write_bytes(strategy_path.read_bytes())
    result['sources']['weekly_strategy'] = {'path': str(strategy_copy), 'sha256': sha256(strategy_copy)}
    collection = json.loads((Path(module_receipt['module_input']).parent / 'COLLECTION-RESULT.json').read_text())
    result['effects']['weekly_collection_total_cost_usd'] = collection['actual_cost_usd']
    report = json.loads((REPORT_ROOT / (report_date.isoformat() + '.json')).read_text())
    policy = json.loads(POLICY_PATH.read_text())
    reporter = load_module('weekly_reporter_module_resume', REPORT_SCRIPT)
    message = render_message(reporter, report, policy, stored['weekly_learning'])
    message += '\nWeekly source stage: ' + module_receipt['status']
    result['discord'] = reporter.post_discord_once(report_date, message)
    result['receipt_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    write_json(receipt_path, result)
    write_json(CONTROL_ROOT / 'latest.json', result)
    result['receipt'] = str(receipt_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date")
    parser.add_argument("--gsc-only", action="store_true",
                        help="Explicit manual GSC aggregate collect-and-review; no weekly strategy or sends")
    parser.add_argument("--module-input", type=Path)
    parser.add_argument("--analysis-only", action="store_true",
                        help="Run evidence-only WS-S modules; no original loop, sends or policy writes")
    parser.add_argument("--analysis-output", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="bypass the week-scoped completed-receipt replay guard")
    args = parser.parse_args(argv)
    if args.gsc_only and (args.force or args.analysis_only or args.analysis_output is not None
                          or args.module_input is not None):
        parser.error("--gsc-only cannot combine with --force, analysis or module inputs")
    if args.analysis_only:
        if args.module_input is None or args.analysis_output is None:
            parser.error("--analysis-only requires --module-input and --analysis-output")
        sys.path.insert(0, str(BUSINESS))
        from ops.glp.seo_weekly_modules import run_modules
        result = run_modules(args.module_input, args.analysis_output)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "PASS_ANALYSIS_ONLY" else 2
    if args.analysis_output is not None:
        parser.error("--analysis-output requires --analysis-only")
    report_date = (dt.date.fromisoformat(args.report_date) if args.report_date
                   else dt.datetime.now(PT).date())
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({
                "machine": "glp_weekly_seo_control", "status": "NOOP_ALREADY_RUNNING",
                "report_date": report_date.isoformat(),
            }, sort_keys=True))
            return 0
        if args.gsc_only:
            try:
                result = gsc_only(report_date)
            except Exception as exc:
                # This branch must not enter the full-weekly Discord/error path.
                print(json.dumps({"machine": "glp_weekly_seo_control", "mode": "gsc_only",
                                  "status": "GSC_ONLY_BLOCKED", "report_date": report_date.isoformat(),
                                  "error": f"{type(exc).__name__}:{str(exc)[:220]}",
                                  "full_weekly_pass": False, "natural_weekly_run_observed": False},
                                 sort_keys=True))
                return 2
            print(json.dumps(result, sort_keys=True))
            return 0
        try:
            result = None if args.force else completed_receipt(report_date)
            if result is None:
                result = execute(report_date, args.module_input) if args.module_input else execute(report_date)
            elif result.get('weekly_modules', {}).get('status') not in {'PASS_WEEKLY_SOURCE_STAGED', 'PASS_ANALYSIS_ONLY'}:
                result = resume_modules(report_date, result)
        except Exception as exc:  # noqa: BLE001
            try:
                reporter = load_module("glp_weekly_reporter_control_error", REPORT_SCRIPT)
                reporter.post_discord_once(report_date, (
                    "🔴 **SEO WEEKLY CONTROL: BLOCKED**\n"
                    "Ranking/strategy report did not complete, so no strategy change is claimed.\n"
                    f"→ {type(exc).__name__}: {str(exc)[:220]}\n"
                    f"-# control root: {CONTROL_ROOT}"
                ))
            except Exception:
                pass
            print(json.dumps({
                "machine": "glp_weekly_seo_control", "status": "FAIL",
                "report_date": report_date.isoformat(),
                "error": f"{type(exc).__name__}:{str(exc)[:220]}",
            }, sort_keys=True))
            return 1
    print(json.dumps({
        "machine": result["machine"], "status": result["status"],
        "report_date": result["report_date"], "verdict": result["verdict"],
        "published": result["published"], "indexed": result["indexed"],
        "clicks": result["clicks"], "targeting_decision": result["targeting"]["decision"],
        "message_id": result["discord"].get("message_id"),
        "idempotent_replay": bool(result.get("idempotent_replay")),
        "receipt": result["receipt"], "receipt_sha256": result["receipt_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
