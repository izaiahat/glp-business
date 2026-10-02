#!/usr/bin/env python3
"""Q25 pinned report-only launcher; scheduler is the sole delivery owner."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Weekly Ads","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"\ud83d\udcca **GLP Weekly Ads** \u2014 report-only; scale remains gate-held\n\ncampaign serving state: **PAUSED** \u00b7 evidence freshness: **current**\nGoogle A3 week spend USD: **0** \u00b7 clicks: **0**\nMeta A3 week spend USD: **UNKNOWN** \u00b7 clicks: **UNKNOWN**\nMicrosoft A3 week spend USD: **UNKNOWN** \u00b7 clicks: **UNKNOWN**\napproved-sale CPA: **UNKNOWN** \u00b7 retained ROAS: **UNKNOWN**\nRotate: **none recommended**; keep A3 creatives PAUSED. LP hypotheses remain staged.\nPostHog: **observed aggregates only**; REAL consented funnel remains UNKNOWN.\n\u2192 Keep spend held; resolve destination, classification and measurement. Spend decision stays with the operator.\n-# report-only / delivery not proven; report: /home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/final-render/ANALYSIS.json","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/test_weekly_ads_loop.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
from pathlib import Path
import hashlib
import subprocess
import sys
PINS = {'/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_cron_entry.py': 'd6cd1bd502ce602c6ff7655200a0191a37a95fd06d6b3688ff87134386ee408e', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py': 'd51b9ea03eddfd72641a4cd79e04d6b76e114e80c645ba6f866a7ca29705d1d1', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json': '62bfd7a9fffdae836ace33f6148e3225aa5ebe1be6853e4c0e59d717a402c5dc', '/home/ubuntu/business/scripts/result_contract.py': 'fca32b1a7b8b7cacb4d1dcf2be5f11b5c450d3d9db450cf730d9bc01c65519f1', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/briefs/plan/A3-CAMPAIGNS-THREE-PLATFORMS.md': 'f5d5343f649080b5f949f44d44c726e7102eb81ccb2e65972dcc7fab98e506eb'}
def main():
    for path, expected in PINS.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Q25_SOURCE_PIN_DRIFT:' + Path(path).name)
    return subprocess.run(['/home/ubuntu/business/ventures/glp-clinic-scout/ads-control-plane/.venv/bin/python',
        '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_cron_entry.py'],
        cwd='/home/ubuntu/business', timeout=930, check=False).returncode
if __name__ == '__main__':
    sys.exit(main())
