#!/usr/bin/env bash
# Hourly PLAN-only coordinator; live native children retain ownership.
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Auto-Coordinator","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **GLP Auto-Coordinator** — deferred to active TUI children\n\nlive child slots: **7** · closed: **0**\n→ current TUI coordinator retains dispatch and closure ownership\n-# artifacts: /home/ubuntu/business/reports/ops/auto-coordinator","fixture_output_class":"alert","fixture_test":"bash -n /home/ubuntu/.hermes/scripts/glp_auto_coordinator.sh","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
set -u
LOCK=/home/ubuntu/.hermes/state/glp_auto_coordinator.lock
exec 9>"$LOCK"
flock -n 9 || exit 0
export GLP_AUTO_COORDINATOR_LOCK_FD=9
cd /home/ubuntu/business || exit 1
OUT=/home/ubuntu/business/reports/ops/auto-coordinator/run-$(date -u +%Y%m%dT%H%M%SZ).log
mkdir -p "$(dirname "$OUT")"
python3 scripts/auto_coordinator_guard.py > "$OUT.guard.json"
GUARD_RC=$?
if [ "$GUARD_RC" -ne 0 ]; then
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print("⚠️ **GLP Auto-Coordinator** — deferred to TUI ownership\n\nstatus: **%s** · live child slots: **%s** · closed: **0**\n→ current TUI coordinator retains dispatch and closure ownership\n-# artifacts: %s" % (d["status"],d.get("live_child_slots","UNKNOWN"),sys.argv[1]))' "$OUT.guard.json"
  exit 0
fi
# No-model decision driver; included OpenAI workers only. It never opens a paid
# fallback, redeems a reset, injects a TUI, installs or enables a scheduler.
# The runner and each fresh leaf reserve their existing shared host leases.
# FD 9 survives through workers; no orphaned effect can lose the singleton.
/home/ubuntu/.hermes/hermes-agent/venv/bin/python /home/ubuntu/business/scripts/auto_coordinator_runner.py > "$OUT" 2>&1 < /dev/null
RC=$?
if [ "$RC" -eq 10 ]; then
  printf '⚠️ **GLP Auto-Coordinator** — deferred by shared host admission\n\nclosed: **0** · dispatches: **0**\n→ current owner retains its live work; inspect the admission readback\n-# artifacts: %s\n' "$OUT"
  exit 0
fi
python3 -c 'import sys;from scripts.auto_coordinator_loop import render_log;print(render_log(sys.argv[1],int(sys.argv[2])))' "$OUT" "$RC"
exit "$RC"
