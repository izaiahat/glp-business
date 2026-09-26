#!/usr/bin/env python3
"""Shared config contract for the report-only SEO/blog machine."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

REQUIRED_TOP_LEVEL = (
    "schema", "site_id", "enabled", "vertical", "analytics", "cloudflare",
    "paths", "program_set", "privacy", "events", "optimizer", "topic_feed", "publishing", "discovery",
    "improvement_loops",
)

# Standing operator authority for zero-approval autonomous release + optimizer
# auto-apply (operator, 2026-08-13: "it should not need any approvals from me
# ever"). Every autonomous contract below must bind to this exact receipt.
AUTONOMOUS_AUTHORITY_RECEIPT = (
    "/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/"
    "OPERATOR-AUTHORITY-AUTONOMOUS-RELEASE-20260813.md"
)
AUTONOMOUS_APPROVAL_MODE = "AUTONOMOUS_MACHINE_GATES"
AUTONOMOUS_MACHINE_CONTRACT = "seo-machine-autonomous-release-v1"
AUTONOMOUS_CADENCE = {"target_per_day": 1, "hard_ceiling_per_day": 3}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(config).encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    raw_digest = config_digest(value)
    failures = validate_config(value)
    if failures:
        raise ValueError("invalid machine config: " + "; ".join(failures))
    if value.get("enabled") and value.get("paths", {}).get("current_content_base_state"):
        try:
            from current_content_base import bind_current_content_base, verify_resolution
        except ModuleNotFoundError:  # package import during pytest
            from scripts.current_content_base import bind_current_content_base, verify_resolution
        value = bind_current_content_base(value)
        try:
            from analytics_feedback import load_route_aliases
        except ModuleNotFoundError:  # package import during pytest
            from scripts.analytics_feedback import load_route_aliases
        load_route_aliases(value)
        verify_resolution(value["_current_content_base_resolution"])
    value["_config_path"] = str(source.resolve())
    value["_config_digest"] = raw_digest
    return value


def get(config: dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            raise KeyError(dotted)
        value = value[key]
    return value


def validate_config(config: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not isinstance(config, dict):
        return ["root_not_object"]
    for key in REQUIRED_TOP_LEVEL:
        if key not in config:
            failures.append(f"missing:{key}")
    if failures:
        return failures
    if config.get("schema") not in {"seo-blog-machine-vertical-config-v1", "seo-blog-machine-clone-template-v1"}:
        failures.append("schema_invalid")
    for dotted in (
        "vertical.id", "vertical.label", "vertical.domain", "vertical.gsc_site_url",
        "analytics.ga4_property_id", "analytics.business_timezone",
        "cloudflare.account_id", "cloudflare.d1_database_id", "cloudflare.d1_database_name", "cloudflare.d1_binding",
        "paths.local_database", "paths.migration", "paths.engagement_migration", "paths.collector_lock",
        "paths.collector_receipts", "paths.optimizer_receipts",
        "paths.topic_queue_dir", "paths.optimizer_history_dir", "paths.improvement_proposal_dir", "paths.discovery_receipts",
        "paths.discovery_seed_ledger",
        "optimizer.policy_version",
        "discovery.schema", "discovery.budget.run_cap_usd",
        "discovery.competitors.head_queries", "discovery.competitors.location_code",
        "discovery.competitors.language_code", "discovery.competitors.device",
        "discovery.competitors.depth", "discovery.competitors.maximum_competitors",
        "discovery.competitors.estimated_cost_usd",
        "discovery.ranked_keywords.endpoint", "discovery.ranked_keywords.maximum_domains", "discovery.ranked_keywords.per_domain_limit",
        "discovery.ranked_keywords.estimated_cost_usd", "discovery.ranked_keywords.hard_kd_max",
        "discovery.seeds.maximum_per_request", "discovery.seeds.proof.minimum_clicks",
        "discovery.seeds.proof.minimum_impressions", "discovery.seeds.proof.minimum_observed_dates",
        "discovery.intent_angles.classes", "discovery.matrix.version", "discovery.matrix.molecules",
    ):
        try:
            value = get(config, dotted)
        except KeyError:
            failures.append(f"missing:{dotted}")
            continue
        if value in (None, "", []):
            failures.append(f"empty:{dotted}")
        if config.get("enabled") and isinstance(value, str) and value.startswith("REQUIRED_"):
            failures.append(f"placeholder_enabled:{dotted}")
    if config.get("enabled"):
        uses_current_content_base = bool(config.get("paths", {}).get("current_content_base_state"))
        if uses_current_content_base:
            for dotted in ("paths.current_content_base_state", "program_set.release_relative_path"):
                try:
                    value = get(config, dotted)
                except KeyError:
                    failures.append(f"missing:{dotted}")
                    continue
                if not isinstance(value, str) or not value:
                    failures.append(f"empty:{dotted}")
        if not isinstance(config.get("paths", {}).get("content_catalogs"), list) or not config["paths"]["content_catalogs"]:
            failures.append("enabled_content_catalogs_empty")
        else:
            for index, catalog in enumerate(config["paths"]["content_catalogs"]):
                if uses_current_content_base:
                    if not isinstance(catalog.get("catalog_id"), str) or not catalog.get("catalog_id"):
                        failures.append(f"enabled_content_catalog_id_missing:{index}")
                    if not isinstance(catalog.get("release_relative_path"), str) or not catalog.get("release_relative_path"):
                        failures.append(f"enabled_content_catalog_relative_path_missing:{index}")
                    if "path" in catalog or "registry_path" in catalog:
                        failures.append(f"enabled_content_catalog_concrete_path_forbidden:{index}")
                elif not isinstance(catalog.get("path"), str) or not catalog.get("path"):
                    failures.append(f"enabled_content_catalog_path_missing:{index}")
                if not isinstance(catalog.get("route_fields"), list) or not catalog.get("route_fields"):
                    failures.append(f"enabled_content_catalog_route_fields_empty:{index}")
                if not isinstance(catalog.get("route_alias_fields"), list) or not catalog.get("route_alias_fields"):
                    failures.append(f"enabled_content_catalog_route_alias_fields_empty:{index}")
        publishing = config.get("publishing", {})
        auto_enabled = publishing.get("auto_generate") is True or publishing.get("auto_publish") is True
        if auto_enabled:
            required = set(publishing.get("required_gates") or [])
            contract_gates = {"operator_topic_go", "serp_brief_complete", "source_pack_complete",
                              "claims_source_bound", "risk_gate_pass", "duplicate_intent_pass",
                              "template_gate_pass", "unique_value_pass", "opus_five_gate_pass",
                              "opus_subject_hash_recheck", "per_article_release_manifest",
                              "production_predecessor_cas", "post_publish_live_readback",
                              "cache_purge_receipt", "gsc_sitemap_readback"}
            if publishing.get("auto_generate") is not True or publishing.get("auto_publish") is not True:
                failures.append("opus_auto_mode_requires_generate_and_publish")
            if publishing.get("approval_mode") == AUTONOMOUS_APPROVAL_MODE:
                # Zero-approval autonomous release: the operator gate is replaced
                # by the in-code machine-gate chain; the standing authority
                # receipt must exist and be pinned, and the cadence must stay at
                # target 1/day with a hard ceiling of 3/day.
                if publishing.get("machine_contract_version") != AUTONOMOUS_MACHINE_CONTRACT:
                    failures.append("autonomous_mode_machine_contract_invalid")
                if publishing.get("authority_receipt") != AUTONOMOUS_AUTHORITY_RECEIPT:
                    failures.append("autonomous_mode_authority_receipt_invalid")
                elif not Path(AUTONOMOUS_AUTHORITY_RECEIPT).is_file():
                    failures.append("autonomous_mode_authority_receipt_missing")
                if publishing.get("cadence") != AUTONOMOUS_CADENCE:
                    failures.append("autonomous_mode_cadence_invalid")
                autonomous_gates = contract_gates | {"stop_seo_kill_switch", "cadence_ceiling",
                                                     "editorial_honesty_contract", "no_medical_reviewer",
                                                     "coverage_invariant", "completeness_sensor"}
                if not autonomous_gates <= required:
                    failures.append("autonomous_mode_required_gates_incomplete")
            else:
                if publishing.get("approval_mode") != "WEEKLY_TOPIC_GO_THEN_OPUS_5_GATE_AUTOPUBLISH":
                    failures.append("opus_auto_mode_approval_contract_invalid")
                if publishing.get("machine_contract_version") != "seo-machine-opus-autopublish-v1":
                    failures.append("opus_auto_mode_machine_contract_invalid")
                if not contract_gates <= required:
                    failures.append("opus_auto_mode_required_gates_incomplete")
        optimizer_config = config.get("optimizer", {})
        if optimizer_config.get("report_only") is not True:
            # report_only false is legal ONLY under the autonomous auto-apply
            # contract bound to the standing authority receipt.
            auto_apply = optimizer_config.get("auto_apply")
            if not isinstance(auto_apply, dict):
                failures.append("optimizer_report_only_off_requires_auto_apply_contract")
            else:
                if auto_apply.get("authority_receipt") != AUTONOMOUS_AUTHORITY_RECEIPT:
                    failures.append("optimizer_auto_apply_authority_receipt_invalid")
                elif not Path(AUTONOMOUS_AUTHORITY_RECEIPT).is_file():
                    failures.append("optimizer_auto_apply_authority_receipt_missing")
                if auto_apply.get("rollback_required") is not True:
                    failures.append("optimizer_auto_apply_rollback_required")
                if auto_apply.get("max_daily_page_churn_per_route") != 1:
                    failures.append("optimizer_auto_apply_churn_cap_invalid")
                allowed_actions = auto_apply.get("allowed_action_types")
                required_actions = {
                    "search_snippet_review", "intro_clarity_review", "title_meta_ctr_rewrite",
                    "internal_link_insertion", "stale_price_line_refresh", "capture_placement_test",
                }
                if (
                    not isinstance(allowed_actions, list)
                    or len(allowed_actions) != len(set(allowed_actions))
                    or not required_actions <= set(allowed_actions)
                ):
                    failures.append("optimizer_auto_apply_action_space_invalid")
                thresholds = optimizer_config.get("thresholds") or {}
                capture_minimum = thresholds.get("capture_placement_min_views")
                capture_floor = thresholds.get("capture_placement_max_completion_rate")
                if not isinstance(capture_minimum, int) or isinstance(capture_minimum, bool) or capture_minimum < 5:
                    failures.append("optimizer_capture_placement_min_views_invalid")
                if not isinstance(capture_floor, (int, float)) or isinstance(capture_floor, bool) or not 0 <= capture_floor <= 1:
                    failures.append("optimizer_capture_placement_completion_rate_invalid")
        feedback = config.get("topic_feed", {}).get("analytics_feedback")
        if not isinstance(feedback, dict):
            failures.append("enabled_analytics_feedback_missing")
        else:
            if feedback.get("required") is not True:
                failures.append("enabled_analytics_feedback_required_must_be_true")
            integer_bounds = {
                "lookback_days": (1, 90),
                "max_staleness_days": (0, 7),
                "refresh_minimum_engaged_sessions": (1, 1000000),
                "refresh_minimum_capture_views": (1, 1000000),
            }
            for key, (minimum, maximum) in integer_bounds.items():
                value = feedback.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                    failures.append(f"analytics_feedback_{key}_invalid")
            caps = ("direct_score_cap", "sibling_score_cap", "refresh_score_cap", "total_topic_adjustment_cap")
            for key in caps:
                value = feedback.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 25:
                    failures.append(f"analytics_feedback_{key}_invalid")
            multiplier = feedback.get("sibling_multiplier")
            loop_b_cap = feedback.get("loop_b_score_cap")
            if not isinstance(loop_b_cap, (int, float)) or isinstance(loop_b_cap, bool) or not 0 < loop_b_cap <= 25:
                failures.append("analytics_feedback_loop_b_score_cap_invalid")
            if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or not 0 <= multiplier <= 1:
                failures.append("analytics_feedback_sibling_multiplier_invalid")
            weights = feedback.get("weights")
            required_weights = {"engaged_session", "affiliate_click", "capture_complete", "refresh_priority"}
            if not isinstance(weights, dict) or set(weights) != required_weights or any(
                not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 10
                for value in (weights or {}).values()
            ):
                failures.append("analytics_feedback_weights_invalid")
        loop_b = config.get("improvement_loops", {}).get("B")
        if not isinstance(loop_b, dict):
            failures.append("improvement_loops_b_missing")
        else:
            components = (
                loop_b.get("decay_priority_points_max"),
                loop_b.get("engagement_priority_points_max"),
            )
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 for value in components):
                failures.append("improvement_loops_b_priority_components_invalid")
            elif sum(float(value) for value in components) > 100:
                failures.append("improvement_loops_b_priority_components_exceed_100")
    discovery = config.get("discovery", {})
    if discovery.get("schema") != "continuous-discovery-config-v1":
        failures.append("discovery_schema_invalid")
    budget_cap = discovery.get("budget", {}).get("run_cap_usd")
    if not isinstance(budget_cap, (int, float)) or isinstance(budget_cap, bool) or not 0 < budget_cap <= 3:
        failures.append("discovery_budget_cap_invalid")
    competitors = discovery.get("competitors", {})
    if not isinstance(competitors.get("head_queries"), list) or not competitors.get("head_queries"):
        failures.append("discovery_head_queries_invalid")
    if competitors.get("device") not in {"desktop", "mobile"}:
        failures.append("discovery_device_invalid")
    for key in ("maximum_competitors", "depth"):
        value = competitors.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            failures.append(f"discovery_competitors_{key}_invalid")
    ranked = discovery.get("ranked_keywords", {})
    if ranked.get("endpoint") != "/v3/dataforseo_labs/google/ranked_keywords/live":
        failures.append("discovery_ranked_endpoint_invalid")
    hard_kd_max = ranked.get("hard_kd_max")
    if not isinstance(hard_kd_max, (int, float)) or isinstance(hard_kd_max, bool) or hard_kd_max != 30:
        failures.append("discovery_hard_kd_max_invalid")
    for key in ("maximum_domains", "per_domain_limit"):
        value = ranked.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            failures.append(f"discovery_ranked_{key}_invalid")
    for section, values in (("competitors", competitors), ("ranked", ranked)):
        estimate = values.get("estimated_cost_usd")
        if not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or estimate <= 0:
            failures.append(f"discovery_{section}_estimated_cost_invalid")
    multipliers = ranked.get("intent_multipliers")
    if not isinstance(multipliers, dict) or "unknown" not in multipliers:
        failures.append("discovery_intent_multipliers_invalid")
    maximum_seeds = discovery.get("seeds", {}).get("maximum_per_request")
    if not isinstance(maximum_seeds, int) or isinstance(maximum_seeds, bool) or not 1 <= maximum_seeds <= 20:
        failures.append("discovery_seed_limit_invalid")
    classes = discovery.get("intent_angles", {}).get("classes")
    required_angles = {"cheapest", "coupon_savings", "oral", "alternatives", "cost_calculator", "comparison", "insurance", "how_to_get", "review"}
    if not isinstance(classes, dict) or not required_angles <= set(classes):
        failures.append("discovery_intent_classes_invalid")
    molecules = discovery.get("matrix", {}).get("molecules")
    if not isinstance(molecules, dict):
        failures.append("discovery_matrix_invalid")
    elif config.get("enabled") and len(molecules) < 2:
        failures.append("enabled_discovery_matrix_requires_two_groups")
    escalation = config.get("optimizer", {}).get("escalation", {})
    escalation_autonomous = (
        escalation.get("operator_approval_required") is False
        and escalation.get("autonomous_authority_receipt") == AUTONOMOUS_AUTHORITY_RECEIPT
    )
    if escalation.get("page_mutations") is not False or not (
        escalation.get("operator_approval_required") is True or escalation_autonomous
    ):
        failures.append("escalation_must_be_nonmutating_and_approval_gated_or_autonomous")
    threshold = escalation.get("impression_threshold")
    if not isinstance(threshold, int) or threshold < 1:
        failures.append("escalation_impression_threshold_invalid")
    privacy = config.get("privacy", {})
    if privacy.get("query_text_mode") != "commercial_allowlist_only":
        failures.append("privacy_query_text_mode_invalid")
    if not privacy.get("commercial_terms") or not privacy.get("query_clusters"):
        failures.append("privacy_vocabulary_missing")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    print(json.dumps({
        "status": "PASS_CONFIG_VALID",
        "site_id": config["site_id"],
        "enabled": config["enabled"],
        "config_digest": config["_config_digest"],
        "auto_generate": config["publishing"]["auto_generate"],
        "auto_publish": config["publishing"]["auto_publish"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
