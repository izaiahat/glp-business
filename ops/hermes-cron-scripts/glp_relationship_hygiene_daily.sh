#!/usr/bin/env bash
# Daily Partnerize relationship hygiene: adjudicate live relationships against
# the current fit gate, withdraw non-fit pending requests, maintain the
# suppression ledger. Bridge build 2026-08-14 (operator foreign-card mandate).
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Relationship Hygiene","output_class":"silence","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"⚠️ **GLP Relationship Hygiene** — daily pass failed before completing\n\nwithdrawals: **0** · suppressions: **0**\n→ inspect the hygiene receipt; no ledger write was partially applied\n-# artifacts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/hygiene","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_glp_relationship_hygiene.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
set -uo pipefail
umask 077
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/glp-relationship-hygiene.lock
chmod 600 /home/ubuntu/.hermes/state/glp-relationship-hygiene.lock
flock -n 9 || exit 0
expected_payload_sha256='a7059b8470240a0fb36cc97a539aff3653599861be70ac776361ad7f4314e189'
expected_filter_sha256='5ea9f39132b6475809d1616a09d4cb859af22ad38d0b5332047fcf4a2b155b6e'
actual_payload="$(sha256sum scripts/glp_relationship_hygiene.py | cut -d' ' -f1)"
actual_filter="$(sha256sum scripts/affiliate_niche_geo_filter.py | cut -d' ' -f1)"
if [[ "$actual_payload" != "$expected_payload_sha256" || "$actual_filter" != "$expected_filter_sha256" ]]; then
  printf '⚠️ **GLP Relationship Hygiene** — payload pin mismatch; run refused\n'
  exit 2
fi
out="$(timeout 550 python3 scripts/glp_relationship_hygiene.py 2>&1)" || {
  printf '⚠️ **GLP Relationship Hygiene** — daily pass failed before completing\n\n%s\n-# artifacts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/hygiene\n' "${out: -400}"
  exit 1
}
# Healthy runs are silent; the summary line lives in the receipt for the daily report.
exit 0
