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
check_source scripts/glp_affiliate_weekly_reporting_j7_job.py f4f0f959bb0e7fd01530faac48aee7af8e5c0fabcaeed73fcf5831efec41eecb
check_source ops/glp/reporting-v2/source-manifest-j7.json e16fbc267069eeedb155a4faca90d853e09224bca5b2c0f58d56b7981ace4a66
check_source ops/glp/reporting-v2/identity-registry.json 0dc42e332009d1438c08318fe36383949c68a059fb0e872eb0fee9d9be7ef513
check_source reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json 9e0bc9ba3f1910e1f5e540bcbfcaa1c344a27366fbc0209ee55972ef4628a799
args=(--live-network-readonly --xentraffic-intake /home/ubuntu/business/reports/ops/fable-plan-2-20261009/lanes/J7/XENTRAFFIC-FINANCIAL-INTAKE.json --xentraffic-sha256 786b3b08e7357bd64e7adb7f58f302e395aafeac14d61ac4e2b8fb30b1cb206e)
if (( $# )); then
  [[ $# == 1 && "$1" == '--dry-run' ]] || exit 64
  args=()
fi
# Retained D1 capture, NOT a fresh receiver export or closed-payout proof.
check_source reports/ops/f12-host-followthrough-20261002/reconciliation-input.json 5ae5aee441a9dbc6c9cb17dfb66fe0c7f28068f68d267ba6707b3c333deb95f1
args+=(--reconciliation-input /home/ubuntu/business/reports/ops/f12-host-followthrough-20261002/reconciliation-input.json)
args+=(--eden-intake /home/ubuntu/business/reports/ops/fable-plan-2-20261009/lanes/J7/eden-candidate-financial-intake.json --eden-intake-sha256 9f8ad5315874e620bb228be2960641b69e43f455826ce3e1ed3c467eb9a3962e)
args+=(--statement-intake /home/ubuntu/business/reports/ops/fable-plan-2-20261009/lanes/J9/ORDERLY-CLOSED-STATEMENT-INTAKE.json --statement-sha256 08d5ae7154462d4f37d39f36cc28332559727996010ff4b7362c0406e141db4d)
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B \
  /home/ubuntu/business/scripts/glp_affiliate_weekly_reporting_j7_job.py \
  --source-manifest /home/ubuntu/business/ops/glp/reporting-v2/source-manifest-j7.json \
  --manifest-sha256 e16fbc267069eeedb155a4faca90d853e09224bca5b2c0f58d56b7981ace4a66 \
  --roster /home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json \
  --identity-registry /home/ubuntu/business/ops/glp/reporting-v2/identity-registry.json \
  --report-root /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/weekly-reporting \
  --lookback-days 90 --max-requests 32 --budget-seconds 240 "${args[@]}"
