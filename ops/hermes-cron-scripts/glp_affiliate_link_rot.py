#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Link Rot","output_class":"alert","composer_type":"deterministic_script","privacy_level":"internal","fixture":"🔴 **GLP Link Rot** — 1 live-link failure\n\nchecked: **18 unique links**\n→ Repair or hold the failed affiliate links before routing traffic.\n-# receipt: /home/ubuntu/business/reports/glp-clinic-scout/excellence-pass-20260801/monitoring/affiliate-link-rot/latest.json","fixture_output_class":"alert","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
import runpy
runpy.run_path('/home/ubuntu/business/scripts/glp_affiliate_link_rot.py', run_name='__main__')
