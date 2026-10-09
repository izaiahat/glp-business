#!/usr/bin/env bash
# Hourly :47 one-command release of merged site main (operator-approved-main-release).
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Main Release","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **GLP Main Release** — release failed, production unchanged\n\nsite main: **00790cde** · error: **route_matrix_did_not_converge**\n→ inspect the run report before the next :47 attempt\n-# artifacts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/registry-site-sync","fixture_output_class":"alert","fixture_test":"bash -n /home/ubuntu/.hermes/scripts/glp_release_main_cron.sh","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
set -u
cd /home/ubuntu/business || exit 1
LOG=/home/ubuntu/business/logs/glp-release-main.log
mkdir -p "$(dirname "$LOG")"
OUT=$(GLP_RUN_TYPE=scheduled timeout 3300 /usr/bin/python3 -B scripts/glp_release_main.py 2>>"$LOG")
RC=$?
printf '%s %s rc=%s\n' "$(date -u +%FT%TZ)" "$OUT" "$RC" >> "$LOG"
STATUS=$(printf '%s' "$OUT" | python3 -c 'import json,sys
try:print(json.loads(sys.stdin.read().strip().splitlines()[-1]).get("status",""))
except Exception:print("")' 2>/dev/null)
case "$RC:$STATUS" in
  0:PASS_ALREADY_CURRENT|0:DEFERRED_BUSY) exit 0 ;;  # success silence
  0:PASS_PRODUCTION_TERMINAL)
    DEP=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.loads(sys.stdin.read().strip().splitlines()[-1]).get("deployment_id",""))')
    printf '✅ **GLP Main Release** — site main published\n\ndeployment: **%s**\n-# artifacts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/registry-site-sync\n' "${DEP:0:8}"
    exit 0 ;;
esac
ERR=$(tail -n 3 "$LOG" | tr '\n' ' ' | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' | cut -c1-300)
printf '⚠️ **GLP Main Release** — release did not complete (rc=%s, status=%s)\n\n%s\n→ inspect the run report before the next :47 attempt\n-# artifacts: /home/ubuntu/business/logs/glp-release-main.log\n' "$RC" "${STATUS:-none}" "$ERR"
exit 0
