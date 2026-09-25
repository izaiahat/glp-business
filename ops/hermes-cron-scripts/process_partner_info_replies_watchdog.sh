#!/usr/bin/env bash
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Partner Info Reply Loop","output_class":"report","composer_type":"deterministic_script","privacy_level":"internal","fixture":"✅ **Partner Info Reply Loop** — no new provider reply\n\n→ Silent until a provider reply is parsed or the loop fails.","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
set -u
cd /home/ubuntu/business
out=$(mktemp)
trap 'rm -f "$out"' EXIT
if ! /usr/bin/python3 scripts/process_partner_info_replies.py --apply >"$out" 2>&1; then
  cat "$out"
  exit 1
fi
/usr/bin/python3 - "$out" <<'PY'
import json,sys
lines=open(sys.argv[1],encoding='utf-8').read().splitlines()
for line in reversed(lines):
    try:
        data=json.loads(line)
        if data.get('status') != 'PASS_WAITING_PROVIDER_REPLY':
            print(json.dumps(data, sort_keys=True))
        break
    except Exception:
        continue
PY
