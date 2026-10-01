#!/usr/bin/env python3
"""Q25 pinned report-only launcher; scheduler is the sole delivery owner."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Weekly Ads","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"\ud83d\udcca **GLP Weekly Ads** \u2014 report-only; scale remains gate-held\n\ncampaign serving state: **PAUSED** \u00b7 evidence freshness: **current**\nGoogle A3 week spend USD: **0** \u00b7 clicks: **0**\nMeta A3 week spend USD: **UNKNOWN** \u00b7 clicks: **UNKNOWN**\nMicrosoft A3 week spend USD: **UNKNOWN** \u00b7 clicks: **UNKNOWN**\napproved-sale CPA: **UNKNOWN** \u00b7 retained ROAS: **UNKNOWN**\nRotate: **none recommended**; keep A3 creatives PAUSED. LP hypotheses remain staged.\nPostHog: **observed aggregates only**; REAL consented funnel remains UNKNOWN.\n\u2192 Keep spend held; resolve destination, classification and measurement. Spend decision stays with the operator.\n-# report-only / delivery not proven; report: /home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/final-render/ANALYSIS.json","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/test_weekly_ads_loop.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
from pathlib import Path
import hashlib
import subprocess
import sys
PINS = {'/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_cron_entry.py': 'd6cd1bd502ce602c6ff7655200a0191a37a95fd06d6b3688ff87134386ee408e', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py': '05bda1aa7f23fc2dca94d35bbcf65276b8bd1e743a9c0e4eb21445867f306653', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json': '705e4128cccf2730d8a0d3c75412278975641350b7c3c6c17b0541b5dc0b0cad', '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A3/RESULT.json': '257339ab71e99ebbf845b56b6e1414d2baef8ec9ee7eb014eaf555f5cff8ce94'}
def main():
    for path, expected in PINS.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Q25_SOURCE_PIN_DRIFT:' + Path(path).name)
    return subprocess.run(['/home/ubuntu/business/ventures/glp-clinic-scout/ads-control-plane/.venv/bin/python',
        '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_cron_entry.py'],
        cwd='/home/ubuntu/business', timeout=930, check=False).returncode
if __name__ == '__main__':
    sys.exit(main())
