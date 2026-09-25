#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/scripts:${PWD}/tests${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q \
  tests/test_capacity_pacing.py \
  tests/test_glp_ads_three_platform_monitor.py \
  tests/test_glp_reddit_pack_scout.py \
  tests/test_glp_program_liveness_check.py \
  tests/test_sync_from_repo.py
