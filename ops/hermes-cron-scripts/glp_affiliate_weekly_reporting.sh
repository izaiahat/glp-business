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
check_source scripts/glp_affiliate_weekly_reporting_job.py ae7cba876c14fd52594a4827d2c9289e264080bd9d72fb355b4dab64b48e57d8
check_source ops/glp/reporting-v2/source-manifest.json 5c022f814c9d3160cd61185b24b952974347689b7dd2349e846bb34e75795da5
check_source ops/glp/reporting-v2/identity-registry.json 0f46cfb248f1d20a7c976ed7baec0c6d816a3371da95bdbc0bca8e21c96cf1f8
check_source reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json 9e0bc9ba3f1910e1f5e540bcbfcaa1c344a27366fbc0209ee55972ef4628a799
args=(--live-cj-readonly)
if (( $# )); then
  [[ $# == 1 && "$1" == '--dry-run' ]] || exit 64
  args=()
fi
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B \
  /home/ubuntu/business/scripts/glp_affiliate_weekly_reporting_job.py \
  --source-manifest /home/ubuntu/business/ops/glp/reporting-v2/source-manifest.json \
  --manifest-sha256 5c022f814c9d3160cd61185b24b952974347689b7dd2349e846bb34e75795da5 \
  --roster /home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json \
  --identity-registry /home/ubuntu/business/ops/glp/reporting-v2/identity-registry.json \
  --report-root /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/weekly-reporting \
  --lookback-days 90 --max-requests 32 --budget-seconds 240 "${args[@]}"
