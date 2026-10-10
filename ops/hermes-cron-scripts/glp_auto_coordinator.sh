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
cd /home/ubuntu/business || exit 1
OUT=/home/ubuntu/business/reports/ops/auto-coordinator/run-$(date -u +%Y%m%dT%H%M%SZ).log
mkdir -p "$(dirname "$OUT")"
python3 scripts/auto_coordinator_guard.py > "$OUT.guard.json"
GUARD_RC=$?
if [ "$GUARD_RC" -ne 0 ]; then
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print("⚠️ **GLP Auto-Coordinator** — deferred to TUI ownership\n\nstatus: **%s** · live child slots: **%s** · closed: **0**\n→ current TUI coordinator retains dispatch and closure ownership\n-# artifacts: %s" % (d["status"],d.get("live_child_slots","UNKNOWN"),sys.argv[1]))' "$OUT.guard.json"
  exit 0
fi
# Included OpenAI executor only: no paid fallback, reset redemption, new support
# sends, maintenance, ad work or publishing outside the PLAN dispatch/close loop.
export LINEAR_ROLE=orchestrator
export GLP_AUTO_COORDINATOR_MODE=plan-dispatch-close-only
# Fast OpenAI model is explicitly configured in the current runtime. The guard
# remains mandatory inside the prompt before each dispatch or closure effect.
timeout --foreground 2400 /home/ubuntu/.hermes/hermes-agent/venv/bin/hermes chat --oneshot \
  --query-file /home/ubuntu/business/ops/linear/AUTO-COORDINATOR.md \
  --provider openai-codex -m gpt-6.1-sol --reasoning high --yolo > "$OUT" 2>&1 < /dev/null
RC=$?
python3 -c 'import re,sys;lines=[re.sub(r"\x1b\[[0-9;]*[a-zA-Z]","",x) for x in open(sys.argv[1])];lines=[x.rstrip() for x in lines if x.strip() and "┊" not in x and "preparing" not in x];print("\n".join(lines[-10:]))' "$OUT"
if [ "$RC" -ne 0 ]; then
  printf '⚠️ **GLP Auto-Coordinator** — run failed\n\nrc: **%s**\n→ inspect the exact run log before retrying\n-# artifacts: %s\n' "$RC" "$OUT"
fi
exit "$RC"
