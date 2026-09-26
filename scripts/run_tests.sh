#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/scripts:${PWD}/tests${PYTHONPATH:+:$PYTHONPATH}"
export SEAT_AUTODISPATCH_TEST_SOURCE="${PWD}/scripts/seat_autodispatch.py"
# Host-only suites depend on live bridge configuration, a bound capture and
# absolute /home/ubuntu/business paths. Run them on the host, never pretend a
# curated GitHub checkout contains those private runtime inputs.
suites=(tests/test_capacity_pacing.py tests/test_glp_program_liveness_check.py tests/test_seat_autodispatch.py tests/test_sync_from_repo.py tests/test_glp316_automated_notices.py tests/test_decisions_router_card.py tests/test_fable_bridge_quota_resume.py)
if [[ -f /home/ubuntu/business/ops/n8n-bridge/actions.json && \
      -f /home/ubuntu/business/scripts/glp_ads_three_platform_monitor.py && \
      -f /home/ubuntu/business/reports/glp-clinic-scout/growth/build-3/reddit-firecrawl-search-captures-20260811-r5.json ]]; then
  suites+=(tests/test_glp_ads_three_platform_monitor.py tests/test_glp_reddit_pack_scout.py)
else
  printf 'HOST_ONLY_SKIPPED: ads monitor and bound Reddit capture suites require live /home/ubuntu/business inputs\n'
fi
python3 -m pytest -q "${suites[@]}"
