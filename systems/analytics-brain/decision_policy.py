#!/usr/bin/env python3
"""Shared analytics gate used by every GLP decision consumer.

The launch activation switch prevents these gates from changing current machine
behaviour while the candidate rebuild is in flight. Once activated, absence,
staleness, or a fail mode blocks the consumer instead of failing open.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUSINESS = HERE.parents[1]
sys.path.insert(0, str(HERE))
from analytics_brain import (  # noqa: E402
    AnalyticsContractError,
    FAIL_MODES,
    digest,
    make_decision,
    utc_now,
)

CONTROL = BUSINESS / "ops/glp/analytics-brain-control.json"
CURRENT_HEALTH = BUSINESS / "reports/glp-clinic-scout/analytics/current/analytics-source-health.json"
DECISION_ROOT = BUSINESS / "reports/glp-clinic-scout/analytics/decisions"
DEFAULT_DB = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"
CONTENT_CONSUMERS = {
    "topic_scorer", "competitor_gap_feed", "strategy_selector", "existing_article_updates",
    "improvement_loops",
}
SEARCH_CONSUMERS = CONTENT_CONSUMERS | {"cro_page_health"}
PROFIT_CONSUMERS = {"provider_profitability", "placement_profitability"}


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalyticsContractError(f"{label}_invalid:{path}") from exc
    if not isinstance(payload, dict):
        raise AnalyticsContractError(f"{label}_not_object:{path}")
    return payload


def control(path: Path = CONTROL) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": "glp-analytics-brain-control-v1", "activated": False,
                "reason": "control_file_missing"}
    payload = read_object(path, "analytics_brain_control")
    if payload.get("schema") != "glp-analytics-brain-control-v1":
        raise AnalyticsContractError("analytics_brain_control_schema_invalid")
    return payload


def consumer_policy(consumer: str, *, decision_class: str = "search",
                    minimum_revenue_sample: int | None = None,
                    control_path: Path = CONTROL, health_path: Path = CURRENT_HEALTH,
                    now: dt.datetime | None = None) -> dict[str, Any]:
    if consumer not in SEARCH_CONSUMERS | PROFIT_CONSUMERS:
        raise AnalyticsContractError(f"analytics_consumer_unknown:{consumer}")
    if decision_class not in {"search", "cro", "profitability"}:
        raise AnalyticsContractError(f"decision_class_invalid:{decision_class}")
    if consumer in CONTENT_CONSUMERS:
        if decision_class != "search":
            raise AnalyticsContractError(
                f"content_consumer_decision_class_invalid:{consumer}:{decision_class}"
            )
        # Content strategy is governed by the outward-search machine (Cogny,
        # GSC, and DataForSEO), not by own-site GA4/PostHog/revenue liveness.
        # Return before reading either control or health so a missing, stale,
        # malformed, or failed analytics receipt can never stop content supply.
        return {
            "mode": "PASS_SEARCH_ONLY",
            "allowed": True,
            "analytics_enforced": False,
            "revenue_blind": True,
            "minimum_revenue_sample": None,
            "observed_revenue_sample": None,
            "reasons": ["content_pipeline_outward_strategy_only"],
            "consumer": consumer,
            "own_site_analytics_ignored": True,
            "required_strategy_sources": ["cogny", "gsc", "dataforseo"],
        }
    state = control(control_path)
    if state.get("activated") is not True:
        return {
            "mode": "PRELAUNCH_INACTIVE",
            "allowed": True,
            "analytics_enforced": False,
            "revenue_blind": True,
            "reasons": [state.get("reason") or "launch_activation_required"],
            "consumer": consumer,
        }
    if not health_path.is_file():
        return {
            "mode": "FAIL_SOURCE_DARK", "allowed": False, "analytics_enforced": True,
            "revenue_blind": True, "reasons": ["analytics_source_health_missing"],
            "consumer": consumer,
        }
    health = read_object(health_path, "analytics_source_health")
    if health.get("schema") != "analytics-source-health-v1":
        return {
            "mode": "FAIL_SCHEMA_DRIFT", "allowed": False, "analytics_enforced": True,
            "revenue_blind": True, "reasons": ["analytics_source_health_schema_invalid"],
            "consumer": consumer,
        }
    try:
        generated = dt.datetime.fromisoformat(str(health.get("generated_at_utc")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {
            "mode": "FAIL_SCHEMA_DRIFT", "allowed": False, "analytics_enforced": True,
            "revenue_blind": True, "reasons": ["analytics_source_health_timestamp_invalid"],
            "consumer": consumer,
        }
    current = now or dt.datetime.now(dt.timezone.utc)
    age_hours = (current - generated.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
    max_age = float(state.get("max_health_age_hours") or 2)
    if age_hours > max_age:
        return {
            "mode": "FAIL_SOURCE_DARK", "allowed": False, "analytics_enforced": True,
            "revenue_blind": True,
            "reasons": [f"analytics_source_health_stale:{age_hours:.2f}h>{max_age:.2f}h"],
            "consumer": consumer,
        }
    source_mode = str(health.get("mode") or "FAIL_SCHEMA_DRIFT")
    if source_mode in FAIL_MODES:
        return {
            "mode": source_mode, "allowed": False, "analytics_enforced": True,
            "revenue_blind": True, "reasons": health.get("mode_reasons") or [],
            "consumer": consumer,
        }
    if decision_class in {"profitability", "cro"}:
        if decision_class == "profitability" and (
                not minimum_revenue_sample or minimum_revenue_sample <= 0):
            raise AnalyticsContractError("profitability_sample_threshold_required")
        if source_mode != "PASS_REVENUE_INFORMED":
            return {
                "mode": "HOLD_REVENUE_OPTIMIZATION", "allowed": False,
                "analytics_enforced": True, "revenue_blind": True,
                "minimum_revenue_sample": minimum_revenue_sample,
                "reasons": [f"source_mode:{source_mode}"], "consumer": consumer,
            }
        raw_observed = health.get("observed_revenue_sample")
        if isinstance(raw_observed, bool) or not isinstance(raw_observed, (int, float)):
            return {
                "mode": "FAIL_SCHEMA_DRIFT", "allowed": False, "analytics_enforced": True,
                "revenue_blind": True, "minimum_revenue_sample": minimum_revenue_sample,
                "observed_revenue_sample": None,
                "reasons": ["observed_revenue_sample_missing_or_invalid"],
                "consumer": consumer,
            }
        observed = int(raw_observed)
        if decision_class == "profitability" and observed < minimum_revenue_sample:
            return {
                "mode": "HOLD_REVENUE_OPTIMIZATION", "allowed": False,
                "analytics_enforced": True, "revenue_blind": True,
                "minimum_revenue_sample": minimum_revenue_sample,
                "observed_revenue_sample": observed,
                "reasons": [f"sample_below_threshold:{observed}<{minimum_revenue_sample}"],
                "consumer": consumer,
            }
    elif source_mode not in {"PASS_SEARCH_ONLY", "PASS_REVENUE_INFORMED"}:
        return {
            "mode": "FAIL_SCHEMA_DRIFT", "allowed": False, "analytics_enforced": True,
            "revenue_blind": True, "reasons": [f"unexpected_pass_mode:{source_mode}"],
            "consumer": consumer,
        }
    return {
        "mode": source_mode,
        "allowed": True,
        "analytics_enforced": True,
        "revenue_blind": source_mode == "PASS_SEARCH_ONLY",
        "minimum_revenue_sample": minimum_revenue_sample,
        "observed_revenue_sample": health.get("observed_revenue_sample"),
        "reasons": health.get("mode_reasons") or [],
        "consumer": consumer,
        "health_receipt": str(health_path),
        "health_receipt_sha256": digest(health),
    }


def require_allowed(policy: dict[str, Any]) -> None:
    if policy.get("allowed") is not True:
        raise AnalyticsContractError(
            f"analytics_decision_blocked:{policy.get('consumer')}:{policy.get('mode')}:"
            + ",".join(str(reason) for reason in policy.get("reasons") or [])
        )


def write_consumer_decision(*, consumer: str, decision_type: str, subject: str, action: str,
                            pre_signal_score: float, components: list[dict[str, Any]],
                            policy: dict[str, Any], minimum_revenue_sample: int | None = None,
                            decision_root: Path = DECISION_ROOT,
                            database: Path = DEFAULT_DB) -> dict[str, Any]:
    outward_content_decision = (
        consumer in CONTENT_CONSUMERS
        and policy.get("mode") == "PASS_SEARCH_ONLY"
        and policy.get("own_site_analytics_ignored") is True
    )
    if policy.get("analytics_enforced") is not True and not outward_content_decision:
        return {
            "status": "PRELAUNCH_INACTIVE",
            "consumer": consumer,
            "recorded": False,
            "revenue_blind": True,
        }
    require_allowed(policy)
    decision = make_decision(
        decision_type=decision_type,
        subject=subject,
        action=action,
        pre_signal_score=pre_signal_score,
        components=components,
        policy=policy,
        minimum_revenue_sample=minimum_revenue_sample,
    )
    payload = {
        "schema": "decision-receipt-v1",
        "generated_at_utc": utc_now(),
        "status": "PASS_DECISIONS_RECORDED",
        "recorded": True,
        "consumer": consumer,
        "data_quality_mode": policy["mode"],
        "revenue_blind": policy["revenue_blind"],
        "decisions": [decision],
    }
    payload["receipt_sha256"] = digest(payload)
    stamp = payload["generated_at_utc"].replace(":", "").replace("-", "")
    path = decision_root / consumer / f"{stamp}-{decision['decision_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    payload["path"] = str(path)
    if database.is_file():
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "analytics_decision_receipt_v1" in tables:
                connection.execute(
                    """INSERT OR IGNORE INTO analytics_decision_receipt_v1
                       (decision_id,decision_type,subject,action,pre_signal_score,components_json,
                        final_score,data_quality_mode,signals_moved_json,
                        unavailable_signals_excluded_json,minimum_revenue_sample,
                        observed_revenue_sample,receipt_sha256,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (decision["decision_id"], decision["decision_type"], decision["subject"],
                     decision["action"], decision["pre_signal_score"],
                     json.dumps(decision["components"], sort_keys=True), decision["final_score"],
                     decision["data_quality_mode"], json.dumps(decision["signals_moved"], sort_keys=True),
                     json.dumps(decision["unavailable_signals_excluded"], sort_keys=True),
                     decision.get("minimum_revenue_sample"), decision.get("observed_revenue_sample"),
                     payload["receipt_sha256"], payload["generated_at_utc"]),
                )
                connection.commit()
        finally:
            connection.close()
    return payload
