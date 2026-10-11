#!/bin/bash
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Fleet ingestion","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **Fleet ingestion** — completion inbox refresh failed\n\nExisting inbox rows and any dispatch receipts are retained; ambiguous effects are never retried blindly.\n→ Repair the deterministic poller from its private error log.\n-# receipt: reports/ops/fleet-inbox/LAST-ERROR.log","fixture_output_class":"alert","fixture_test":"python3 -m unittest discover -s tests -p test_fleet_ingest.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
set -uo pipefail
cd /home/ubuntu/business || exit 70
mkdir -p reports/ops/fleet-inbox
# Pin the approved interpreter; scheduler PATH may otherwise select host Python.
PYTHON=/home/ubuntu/.hermes/hermes-agent/venv/bin/python3
timeout --foreground --signal=TERM --kill-after=10 240 "$PYTHON" /home/ubuntu/business/scripts/fleet_ingest.py 2>reports/ops/fleet-inbox/LAST-ERROR.log
rc=$?
# The existing fleet driver consumes native registrations. DOT1 adds the durable
# Linear/ops-alerts/coordinator handoff, without accepting or merging any output.
if [ "$rc" -eq 0 ]; then
    timeout --foreground --signal=TERM --kill-after=10 120 "$PYTHON" -B /home/ubuntu/business/scripts/dot_lane.py ingest 2>>reports/ops/fleet-inbox/LAST-ERROR.log
    rc=$?
    if [ "$rc" -eq 0 ]; then
        timeout --foreground --signal=TERM --kill-after=10 120 "$PYTHON" -B /home/ubuntu/business/scripts/dot_lane.py refill 2>>reports/ops/fleet-inbox/LAST-ERROR.log
        rc=$?
    fi
fi
if [ "$rc" -ne 0 ]; then
    printf '🔴 **Fleet ingestion** — completion inbox refresh failed\n\nExisting inbox rows and any dispatch receipts are retained; ambiguous effects are never retried blindly.\n→ Repair the deterministic poller from its private error log.\n-# receipt: reports/ops/fleet-inbox/LAST-ERROR.log\n'
fi
exit "$rc"
