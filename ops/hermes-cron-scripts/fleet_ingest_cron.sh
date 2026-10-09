#!/bin/bash
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Fleet ingestion","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **Fleet ingestion** — completion inbox refresh failed\n\nNo issue was closed and no executor was redispatched. Existing inbox rows are retained.\n→ Repair the deterministic poller from its private error log.\n-# receipt: reports/ops/fleet-inbox/LAST-ERROR.log","fixture_output_class":"alert","fixture_test":"python3 -m unittest discover -s tests -p test_fleet_ingest.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
set -uo pipefail
cd /home/ubuntu/business || exit 70
mkdir -p reports/ops/fleet-inbox
timeout --foreground --signal=TERM --kill-after=10 240 python3 /home/ubuntu/business/scripts/fleet_ingest.py 2>reports/ops/fleet-inbox/LAST-ERROR.log
rc=$?
if [ "$rc" -ne 0 ]; then
    printf '🔴 **Fleet ingestion** — completion inbox refresh failed\n\nNo issue was closed and no executor was redispatched. Existing inbox rows are retained.\n→ Repair the deterministic poller from its private error log.\n-# receipt: reports/ops/fleet-inbox/LAST-ERROR.log\n'
fi
exit "$rc"
