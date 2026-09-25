#!/usr/bin/env bash
set -euo pipefail
umask 077
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Affiliate Daily Ops","output_class":"actionable","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **GLP Affiliate Ops** — 1 blocker needs operator action\n\nblocked: **1** · provider writes: **0**\n→ complete the named identity or MFA step from the private blocker receipt\n-# state: operator-blockers.json","fixture_output_class":"actionable","fixture_test":"python3 -m pytest -q tests/test_glp_affiliate_daily_decisions_report.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
LOCK=/home/ubuntu/.hermes/state/glp-affiliate-daily-report.lock
exec 9>"$LOCK"
flock -n 9 || exit 0
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0
set +e
out="$(cd /home/ubuntu/business && /usr/bin/python3 scripts/glp_affiliate_daily_decisions_report.py --cycle 2>&1)"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  failure_tail="$out"
  if [[ ${#failure_tail} -gt 2000 ]]; then failure_tail="${failure_tail: -2000}"; fi
  printf '⚠️ **Affiliate Daily Ops** — cycle failed (rc %s)\n\n%s\n' "$rc" "$failure_tail"
  exit "$rc"
fi
exit 0
