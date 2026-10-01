#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/scripts:${PWD}/tests${PYTHONPATH:+:$PYTHONPATH}"
export SEAT_AUTODISPATCH_TEST_SOURCE="${PWD}/scripts/seat_autodispatch.py"
# Host-only suites depend on live bridge configuration, a bound capture and
# absolute /home/ubuntu/business paths. Run them on the host, never pretend a
# curated GitHub checkout contains those private runtime inputs.
suites=(tests/test_glp_seo_skill_loader.py tests/test_linear_ordinary_completion.py tests/test_gpt6pro_relay_binding.py tests/test_awin_pin_shell_conventions.py tests/test_awin_application_fit_exclusions.py tests/test_capacity_pacing.py tests/test_glp_program_liveness_check.py tests/test_seat_autodispatch.py tests/test_sync_from_repo.py tests/test_glp316_automated_notices.py tests/test_decisions_router_card.py tests/test_fable_bridge_quota_resume.py tests/test_fable_bridge_oversize.py tests/test_glp319_incomplete_period.py)
suites+=(tests/test_glp42_package_only.py tests/test_glp43_current_release.py)
suites+=(tests/test_therapeutic_taxonomy_refresh.py)
suites+=(tests/test_glp44_weekly_gsc_aggregate.py tests/test_glp44_gsc_only_consumer.py)
suites+=(tests/test_glp44_bridge_source_admission.py tests/test_glp44_bridge_fence_successor.py)
suites+=(tests/test_glp44_weekly_pin_adoption.py)
suites+=(tests/test_glp44_reporting_source_pin_pair.py tests/test_glp44_scoped_adoption.py)
suites+=(tests/test_glp44_reader_exclusion.py)
suites+=(tests/test_candidate_freshness_horizon.py)
suites+=(tests/test_glp_landed_uncertain_deployment_reconcile.py)
suites+=(tests/test_glp193_capture_source_binding.py)
suites+=(tests/test_glp196_price_index_contract.py)
suites+=(tests/test_fable_mention_recovery.py)
suites+=(tests/test_glp324_unassigned_commission_preflight.py)
suites+=(tests/test_glp186_binding_diagnostics.py)
suites+=(tests/test_glp183_builder_checked_as_of.py)
suites+=(tests/test_glp_awin_portal_login.py tests/test_awin_fresh_preflight.py)
suites+=(tests/test_maxbounty_pin_release.py)
suites+=(tests/test_glp_maxbounty_fresh_probe.py)
suites+=(tests/test_glp328_outreach_policy.py tests/test_glp328_readonly_evidence.py tests/test_glp328_disabled_reply_connection.py)
suites+=(tests/test_glp76_updater_publication_date.py)
suites+=(tests/test_glp73_current_pointer_consumer.py tests/test_glp73_weekly_actual_entry.py)
suites+=(tests/test_glp250_partner_email_hold.py)
suites+=(tests/test_glp96_research_only_contract.py)
suites+=(tests/test_glp329_add_approved_row_full_grid_dry_run.py)
suites+=(tests/test_glp85_cj_reporting_identity.py)
suites+=(tests/test_glp320_readonly_grouping.py)
suites+=(tests/test_affiliate_discovery_resume.py tests/test_affiliate_discovery_admission.py tests/test_affiliate_best_terms_recovery.py tests/test_affiliate_terms_fanout.py)
suites+=(tests/test_receive_one_discord_message.py tests/test_rook_mcp_events.py tests/test_rook_communication.py)
if [[ -f /home/ubuntu/business/ops/n8n-bridge/actions.json && \
      -f /home/ubuntu/business/scripts/glp_ads_three_platform_monitor.py && \
      -f /home/ubuntu/business/reports/glp-clinic-scout/growth/build-3/reddit-firecrawl-search-captures-20260811-r5.json ]]; then
  suites+=(tests/test_glp_ads_three_platform_monitor.py tests/test_glp_reddit_pack_scout.py)
else
  printf 'HOST_ONLY_SKIPPED: ads monitor and bound Reddit capture suites require live /home/ubuntu/business inputs\n'
fi
python3 -m pytest -q "${suites[@]}"
