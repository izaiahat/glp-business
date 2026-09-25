#!/usr/bin/env bash
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Affiliate Backrun","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **Affiliate Backrun** — 2 approved programs are missing from the sheet\n\nrows dispositioned: **728/728** · missed approvals: **2**\n→ inspect the coverage receipt and wire or sister-queue the missed programs\n-# artifacts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_affiliate_backrun.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/affiliate-backrun.lock
flock -n 9 || exit 0
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0
set +e
OUT="$(python3 scripts/affiliate_backrun.py 2>&1)"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  failure_tail="$OUT"
  if [[ ${#failure_tail} -gt 2000 ]]; then failure_tail="${failure_tail: -2000}"; fi
  printf '⚠️ **Affiliate Backrun** — disposition coverage failed (rc %s)\n\n%s\n' "$rc" "$failure_tail"
  exit "$rc"
fi
python3 - "$OUT" <<'PY'
import json, sys
doc=json.loads(sys.argv[1])
missed=int(doc.get('missed_approvals') or 0)
if missed:
    print(f"⚠️ **Affiliate Backrun** — {missed} current-source-proven GLP-fit joined program(s) missing from the sheet")
PY
