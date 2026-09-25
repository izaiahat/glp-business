#!/usr/bin/env python3
"""Native cron carrier for the verified read-only capacity monitor."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Subscription capacity","output_class":"report","composer_type":"deterministic","privacy_level":"internal","fixture":"📊 **Subscription capacity** — Usage and reset ledger\n\nOpenAI primary: available; backup: depleted.\nClaude: depleted in CLI snapshot.\n→ continue useful OpenAI work; banked resets remain operator-only\n-# ledger: ops/subscription-capacity/latest.json","fixture_output_class":"report","fixture_test":"tests/test_subscription_capacity_monitor.py::test_report_labels_unverified_claude_and_renders_pacific_reset","route":"discord:1487300132054765629","detail_policy":"complete_summary_in_discord"}
import runpy
import sys
sys.path.insert(0, "/home/ubuntu/business/scripts")
runpy.run_path("/home/ubuntu/business/scripts/subscription_capacity_monitor.py", run_name="__main__")
