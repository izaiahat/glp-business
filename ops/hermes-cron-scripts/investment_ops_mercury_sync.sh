#!/usr/bin/env bash
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Mercury Sync","output_class":"event","composer_type":"formatter","privacy_level":"private-local-only","fixture":"💰 **Mercury Sync** — 1 new event captured\n\nsync status: **failed** · money movement: **none**\nprivate bodies shown: **false**\n→ reply **RETRY MERCURY** to rerun safely\n-# local financial details withheld","fixture_output_class":"event","fixture_test":"/home/ubuntu/business/investment-ops/tests/test_mercury_connector.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
set -euo pipefail

ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT
chmod 600 "$ERR"

if /home/ubuntu/business/investment-ops/scripts/cron_mercury_sync.sh >/dev/null 2>"$ERR"; then
  exit 0
fi

printf '%s\n' \
  '💰 **Mercury Sync** — 1 new event captured' \
  '' \
  'sync status: **failed** · money movement: **none**' \
  'private bodies shown: **false**' \
  '→ reply **RETRY MERCURY** to rerun safely' \
  '-# local financial details withheld'
