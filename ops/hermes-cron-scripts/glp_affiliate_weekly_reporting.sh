#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
# K3 bounded read-only reporting. No sender, cash adapter or legacy cutover.
check_source() {
  local actual
  actual="$(/usr/bin/sha256sum -- "$1")"
  [[ "${actual%% *}" == "$2" ]] || { printf 'K3_REPORTING_SOURCE_DRIFT\n' >&2; exit 66; }
}
check_source scripts/glp_affiliate_weekly_reporting_job.py 79e27c066307249971c5afc69dc2fd08dea76245f4267ecb5c084b16795ffd18
check_source ops/glp/reporting-v2/source-manifest.json 62bafd3adae11c82b67115365b455fe07892a9b2c91ccb6442661c7b1188393e
check_source ops/glp/reporting-v2/identity-registry.json 93aa4fa83cba6ee24c81504fe0e58133c87014cb7bb618e1ff9140ab66b21271
check_source reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json 9e0bc9ba3f1910e1f5e540bcbfcaa1c344a27366fbc0209ee55972ef4628a799
args=(--live-network-readonly)
if (( $# )); then
  [[ $# == 1 && "$1" == '--dry-run' ]] || exit 64
  args=()
fi
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B \
  /home/ubuntu/business/scripts/glp_affiliate_weekly_reporting_job.py \
  --source-manifest /home/ubuntu/business/ops/glp/reporting-v2/source-manifest.json \
  --manifest-sha256 62bafd3adae11c82b67115365b455fe07892a9b2c91ccb6442661c7b1188393e \
  --roster /home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json \
  --identity-registry /home/ubuntu/business/ops/glp/reporting-v2/identity-registry.json \
  --report-root /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/weekly-reporting \
  --lookback-days 90 --max-requests 32 --budget-seconds 240 "${args[@]}"
