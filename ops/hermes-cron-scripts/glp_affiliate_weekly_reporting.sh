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
check_source scripts/glp_affiliate_weekly_reporting_job.py 8888f3d1c342267abbdffd70e08bf490eb3c2e4c83199e909d0ba9723d685949
check_source ops/glp/reporting-v2/source-manifest.json c4770afc0ac7b58aca415363012009add2c1cb672eaccc2dfa0a2afe00e7560e
check_source ops/glp/reporting-v2/identity-registry.json 0dc42e332009d1438c08318fe36383949c68a059fb0e872eb0fee9d9be7ef513
check_source reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json 9e0bc9ba3f1910e1f5e540bcbfcaa1c344a27366fbc0209ee55972ef4628a799
args=(--live-network-readonly)
if (( $# )); then
  [[ $# == 1 && "$1" == '--dry-run' ]] || exit 64
  args=()
fi
# Retained D1 capture, NOT a fresh receiver export or closed-payout proof.
check_source reports/ops/f12-host-followthrough-20261002/reconciliation-input.json 5ae5aee441a9dbc6c9cb17dfb66fe0c7f28068f68d267ba6707b3c333deb95f1
args+=(--reconciliation-input /home/ubuntu/business/reports/ops/f12-host-followthrough-20261002/reconciliation-input.json)
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B \
  /home/ubuntu/business/scripts/glp_affiliate_weekly_reporting_job.py \
  --source-manifest /home/ubuntu/business/ops/glp/reporting-v2/source-manifest.json \
  --manifest-sha256 c4770afc0ac7b58aca415363012009add2c1cb672eaccc2dfa0a2afe00e7560e \
  --roster /home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json \
  --identity-registry /home/ubuntu/business/ops/glp/reporting-v2/identity-registry.json \
  --report-root /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/weekly-reporting \
  --lookback-days 90 --max-requests 32 --budget-seconds 240 "${args[@]}"
