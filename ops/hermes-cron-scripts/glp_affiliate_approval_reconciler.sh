#!/usr/bin/env bash
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Affiliate Reconciler","output_class":"silence","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **Affiliate Reconciler** — 1 email needs identity or provider repair\n\nprovider-backed updates: **0** · blocked: **1**\n→ inspect the private reconciliation artifact; no Sheet guess or provider mutation was made\n-# artifacts: /home/ubuntu/business/reports/glp-affiliate-auto-reconciler","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_affiliate_approval_auto_reconciler.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
cd /home/ubuntu/business
# GLP-209 (2026-09-13): the S16/Q47 payload sha pins were REMOVED here.
# This wrapper is named in the 2026-08-20 internal-cron governance strip
# (tests/test_internal_cron_governance_strip.py): an internal observation/queue
# lane's payload may change under its own test suite without re-pinning the
# wrapper. Re-adding the pins re-created exactly the failure that rule exists to
# prevent - affiliate_approval_auto_reconciler.py changed on 2026-09-12 00:19Z
# and this job then failed with SOURCE_DRIFT every 10 minutes for ~25 hours, so
# no affiliate approval was reconciled in that window. Change control for the
# payload lives in tests/test_affiliate_approval_auto_reconciler.py (126 tests).
# Wrappers that can cause an EXTERNAL effect stay byte-pinned on purpose and are
# unaffected by this: glp_awin_drain_sheet_queue.sh (Sheet writes) and
# awin_auth_liveness_preflight.sh (provider identity) keep their drift guards.
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0
# Preview flags must never silently become a live invitation run.
if (( $# )); then
  [[ "$1" == "--dry-run" ]] || { printf 'Only --dry-run arguments are accepted\n' >&2; exit 64; }
  exec /usr/bin/python3.12 scripts/affiliate_approval_auto_reconciler.py "$@"
fi
exec 9>/home/ubuntu/.hermes/state/glp-affiliate-approval-reconciler.lock
chmod 600 /home/ubuntu/.hermes/state/glp-affiliate-approval-reconciler.lock
flock -n 9 || exit 0
invitation_output="$(/usr/bin/python3.12 scripts/affiliate_application_invitation_reconciler.py)" || {
  rc=$?; [[ -z "$invitation_output" ]] || printf '%s\n' "$invitation_output"; exit "$rc"
}
[[ -z "$invitation_output" ]] || printf '%s\n' "$invitation_output"
approval_output="$(/usr/bin/python3.12 scripts/affiliate_approval_auto_reconciler.py)" || {
  rc=$?; [[ -z "$approval_output" ]] || printf '%s\n' "$approval_output"; exit "$rc"
}
[[ -z "$approval_output" ]] || printf '%s\n' "$approval_output"
if [[ -z "$invitation_output" && -z "$approval_output" ]]; then
  printf '[SILENT]\n**Status:** ok\n{"schema":"glp-affiliate-reconciler-heartbeat-v1","status":"ok"}\n'
fi
