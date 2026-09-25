#!/usr/bin/env bash
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP G-6","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"📊 **GLP G-6** — traction remains HOLD as of 2026-07-16\n\npages: **3** · indexed samples: **4** · GSC 28d: **0.0 clicks / 4.0 impressions**\nGA4 28d: **0** · approved: **1** · live links: **0** · revenue: **$0**\n→ hold scale-up; continue approval-gated onboarding and measured indexation\n-# report: /home/ubuntu/business/reports/glp-directory-flywheel-2026-07-07/weekly-traction-reports/2026-07-16.json","fixture_output_class":"report","fixture_test":"python3 /home/ubuntu/business/scripts/glp_g6_weekly_traction_report.py --as-of 2026-07-16","route":"discord:1487300132054765629","detail_policy":"complete_summary_in_discord"}
set -euo pipefail
exec python3 /home/ubuntu/business/scripts/glp_g6_weekly_traction_report.py
