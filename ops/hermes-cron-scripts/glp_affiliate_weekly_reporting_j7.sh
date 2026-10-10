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
check_source scripts/glp_affiliate_weekly_reporting_j7_job.py 690ce66c4cfcd24b72dfe022e737df253098d1804673326709a45fcc2401f0b7
check_source ops/glp/reporting-v2/source-manifest-j7.json 5cba43b8b18c43354e726af007703c937a2b343ee127e51a1d9a0a5f31ae317e
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
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B \
  /home/ubuntu/business/scripts/glp_affiliate_weekly_reporting_j7_job.py \
  --source-manifest /home/ubuntu/business/ops/glp/reporting-v2/source-manifest-j7.json \
  --manifest-sha256 5cba43b8b18c43354e726af007703c937a2b343ee127e51a1d9a0a5f31ae317e \
  --roster /home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json \
  --identity-registry /home/ubuntu/business/ops/glp/reporting-v2/identity-registry.json \
  --report-root /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/weekly-reporting \
  --lookback-days 90 --max-requests 32 --budget-seconds 240 "${args[@]}"
