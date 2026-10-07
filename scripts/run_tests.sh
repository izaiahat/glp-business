#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/scripts:${PWD}/tests${PYTHONPATH:+:$PYTHONPATH}"
export SEAT_AUTODISPATCH_TEST_SOURCE="${PWD}/scripts/seat_autodispatch.py"
# Host-only suites depend on live bridge configuration, a bound capture and
# absolute /home/ubuntu/business paths. Run them on the host, never pretend a
# curated GitHub checkout contains those private runtime inputs.
suites=(tests/test_awin_paused_registration_release.py tests/test_glp_seo_skill_loader.py tests/test_linear_ordinary_completion.py tests/test_gpt6pro_relay_binding.py tests/test_awin_pin_shell_conventions.py tests/test_awin_application_fit_exclusions.py tests/test_capacity_pacing.py tests/test_glp_program_liveness_check.py tests/test_seat_autodispatch.py tests/test_sync_from_repo.py tests/test_glp316_automated_notices.py tests/test_decisions_router_card.py tests/test_fable_bridge_quota_resume.py tests/test_fable_bridge_oversize.py tests/test_glp319_incomplete_period.py)
suites+=(tests/test_glp42_package_only.py tests/test_glp43_current_release.py)
suites+=(tests/test_glp_ship.py tests/test_glp_ship_provenance.py tests/test_glp_ship_retention.py tests/test_glp_ship_runtime.py tests/test_glp_ship_discovery.py tests/test_glp_ship_discovery_install.py tests/test_glp_ship_collector.py)
suites+=(tests/test_glp_ship_publication.py tests/test_source_lock_owner.py)
suites+=(tests/test_therapeutic_taxonomy_refresh.py)
suites+=(tests/test_glp44_weekly_gsc_aggregate.py tests/test_glp44_gsc_only_consumer.py)
suites+=(tests/test_glp44_bridge_source_admission.py tests/test_glp44_bridge_fence_successor.py)
suites+=(tests/test_glp44_weekly_pin_adoption.py)
suites+=(tests/test_glp75_weekly_producer_repairs.py)
suites+=(tests/test_f07_registered_weekly_entry.py tests/test_glp44_reporting_source_pin_pair.py tests/test_glp44_scoped_adoption.py tests/test_glp333_reporting_generation.py tests/test_glp333_glp44_current_generation.py)
suites+=(tests/test_glp334_conversion_reconciliation.py)
suites+=(tests/test_pr148_reporting_followups.py tests/test_api_collectors_partnerstack_admitad.py tests/test_api_collectors_refersion.py tests/test_api_collectors_clickbank_healthtrader.py tests/test_api_collectors_wiring.py tests/test_api_collectors_smoke.py)
suites+=(tests/test_api_collectors_everflow.py)
suites+=(tests/test_glp345_a4_adoption.py)
suites+=(tests/test_glp331_dependency_generation.py tests/test_glp331_carrier_generation.py tests/test_glp337_shared_bridge_generation.py)
suites+=(tests/test_glp44_reader_exclusion.py)
suites+=(tests/test_glp44_collector_release.py tests/test_glp44_collector_successor.py)
suites+=(tests/test_glp341_native_registry_lock.py)
suites+=(tests/test_maintenance_inventory.py tests/test_maintenance_window.py tests/test_maintenance_window_lock_contention.py tests/test_maintenance_keeper_contention.py tests/test_maintenance_adoptions.py)
suites+=(tests/test_maintenance_restoration_review.py tests/test_maintenance_admission_review.py tests/test_maintenance_native_admission.py tests/test_maintenance_cohort_recovery.py tests/test_glp73_sync_rebaseline.py)
suites+=(scripts/test_maintenance_recovery.py)
suites+=(tests/test_adoption_cache_restore.py)
suites+=(tests/test_candidate_freshness_horizon.py)
suites+=(tests/test_glp_landed_uncertain_deployment_reconcile.py)
suites+=(tests/test_glp193_capture_source_binding.py)
suites+=(tests/test_pipeline_tool_contracts.py)
suites+=(tests/test_glp196_price_index_contract.py tests/test_current_release_acceptance.py)
suites+=(tests/test_fable_parent_handoff.py)
suites+=(tests/test_fable_mention_recovery.py tests/test_glp73_scoped_reviewer_routing.py)
suites+=(tests/test_glp324_unassigned_commission_preflight.py)
suites+=(tests/test_glp186_binding_diagnostics.py)
suites+=(tests/test_glp183_builder_checked_as_of.py)
suites+=(tests/test_glp_awin_portal_login.py tests/test_awin_fresh_preflight.py)
suites+=(tests/test_glp268_fresh_browser_callers.py)
suites+=(tests/test_maxbounty_pin_release.py)
suites+=(tests/test_glp_maxbounty_fresh_probe.py)
suites+=(tests/test_glp328_outreach_policy.py tests/test_glp328_readonly_evidence.py tests/test_glp328_disabled_reply_connection.py tests/test_glp_sponsor_campaign.py tests/test_glp_sponsor_boundary_repairs.py tests/test_glp_sponsor_continuation.py tests/test_glp_sponsor_paypal.py)
suites+=(tests/test_glp76_updater_publication_date.py)
suites+=(tests/test_glp73_current_pointer_consumer.py tests/test_glp73_weekly_actual_entry.py tests/test_glp73_builder_current_base.py)
suites+=(tests/test_glp110_prepared_authority_cache.py tests/test_repin_reports_archiver_dependents.py)
suites+=(tests/test_glp73_archive_hardlink_manifest.py tests/test_glp110_restore_pruned_execution.py tests/test_f13_atop_launcher_pin.py)
suites+=(tests/test_glp250_partner_email_hold.py)
suites+=(tests/test_glp96_research_only_contract.py tests/test_glp96_published_intake.py)
suites+=(tests/test_glp329_add_approved_row_full_grid_dry_run.py)
suites+=(tests/test_glp85_cj_reporting_identity.py)
suites+=(tests/test_glp320_readonly_grouping.py tests/test_agent_exec_routing.py)
suites+=(systems/seo-blog-machine/tests/test_workweek_boundary.py)
suites+=(tests/test_glp_affiliate_registry_sync.py tests/test_glp_affiliate_release.py tests/test_glp_affiliate_absence_audit.py)
suites+=(tests/test_affiliate_discovery_resume.py tests/test_affiliate_discovery_admission.py tests/test_affiliate_best_terms_recovery.py tests/test_affiliate_terms_fanout.py tests/test_pro_opaque_program_identity.py)
suites+=(tests/test_receive_one_discord_message.py tests/test_rook_mcp_events.py tests/test_rook_communication.py)
if [[ -f /home/ubuntu/business/ops/n8n-bridge/actions.json && \
      -f /home/ubuntu/business/scripts/glp_ads_three_platform_monitor.py && \
      -f /home/ubuntu/business/reports/glp-clinic-scout/growth/build-3/reddit-firecrawl-search-captures-20260811-r5.json ]]; then
  suites+=(tests/test_glp_ads_three_platform_monitor.py tests/test_glp_reddit_pack_scout.py)
else
  printf 'HOST_ONLY_SKIPPED: ads monitor and bound Reddit capture suites require live /home/ubuntu/business inputs\n'
fi
suites+=(tests/test_glp75_topic_supply_inputs.py)
suites+=(tests/test_glp343_topic_first_party_scoring.py tests/test_glp343_integration_guards.py)
suites+=(tests/test_affiliate_catalog_batch.py)
suites+=(tests/test_n8n_execution_budget.py tests/test_n8n_execution_budget_review.py)
suites+=(tests/test_glp109_scheduler_health.py tests/test_glp320_host_failure_sensor.py)
python3 -m pytest -q "${suites[@]}"
