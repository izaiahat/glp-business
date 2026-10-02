#!/usr/bin/env python3
"""Weekly best-collectible affiliate offer census and staging wrapper."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Best Terms","output_class":"alert","composer_type":"script","privacy_level":"internal","fixture":"⚠️ **Best Terms** — census PARTIAL · 7 marketplace failures\n\nwinners changed: **0** · switches staged: **0**\n→ Review named census failures; no links changed, re-pointed, or deployed.\n-# receipt: reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825/latest-run.json","fixture_output_class":"alert","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BUSINESS = Path(__file__).resolve().parents[2]
MACHINE = BUSINESS / "scripts/affiliate_best_terms_machine.py"


def main() -> int:
    try:
        proc = subprocess.run(
            [sys.executable, str(MACHINE), "--stage", "--resume-census", "--stdout", "discord"],
            capture_output=True,
            text=True,
            timeout=3300,
            cwd=BUSINESS,
        )
    except subprocess.TimeoutExpired:
        print(
            "🔴 **Best Terms** — weekly census exceeded 55 minutes\n\n"
            "live link changes **0** · deploys **0** · run failed closed\n"
            "→ Inspect marketplace collector health before relying on this census.\n"
            "-# source: weekly best terms wrapper · restricted"
        )
        return 2
    output = (proc.stdout or "").strip()
    if output:
        print(output)
    elif proc.returncode != 0:
        print(
            "🔴 **Best Terms** — weekly machine failed without a usable receipt\n\n"
            "live link changes **0** · deploys **0** · run failed closed\n"
            "→ Inspect the private cron log and latest local machine receipt.\n"
            "-# source: weekly best terms wrapper · restricted"
        )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
