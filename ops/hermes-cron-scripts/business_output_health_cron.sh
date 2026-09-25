#!/bin/bash
# Fleet-wide OUTPUT health — did each machine PRODUCE, not just run?
# Silent when all 9 GLP checks are green; loud with exact red lines otherwise.
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Output Health","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **Output Health** — 3 GLP checks unhealthy\n\n**seo_article_buffer** — expected >=3 pre-validated articles ready; actual: 0 ready\n**seo_update_carrier** — expected latest R2 refresh run not BLOCKED; actual: BLOCKED\n**affiliate_site_deploy** — expected PASS and receipt age <=36h; actual: receipt age UNKNOWN\n→ inspect each failed or missing evidence path; run state alone is not output\n-# 6/9 healthy · receipt: /home/ubuntu/business/reports/ops/output-health-latest.json","fixture_output_class":"alert","fixture_test":"python3 -c \"import ast;ast.parse(open('/home/ubuntu/business/scripts/business_output_health_sensor.py').read())\"","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
set -uo pipefail
BUSINESS=/home/ubuntu/business
cd "$BUSINESS" || exit 70
readonly SENSOR="$BUSINESS/scripts/business_output_health_sensor.py"
python3 "$SENSOR"
exit 0
