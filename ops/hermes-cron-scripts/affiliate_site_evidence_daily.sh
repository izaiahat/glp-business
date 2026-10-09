#!/usr/bin/env bash
# GLP-350 bounded map-first affiliate evidence intake.
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Affiliate Site Evidence","output_class":"event","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"💰 **Affiliate Site Evidence** — scheduled harvest completed\n\nselected: 1 · fetched: 1 · applications: 0\n-# receipts: local site-evidence-harvest","fixture_output_class":"event","fixture_test":"python3 -m pytest -q tests/test_firecrawl_glp350_cron.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/affiliate-site-evidence.lock
flock -n 9 || exit 0
exec 8>/home/ubuntu/.hermes/state/affiliate-supply-sweep.lock
flock -w 900 8 || exit 75
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RECEIPT="reports/glp-clinic-scout/affiliate-machine/site-evidence-harvest/daily-${STAMP}/site-evidence-harvest.json"
OUT="$(/home/ubuntu/business/.venv-affiliate-blitz/bin/python scripts/affiliate_site_evidence.py --limit 40 --max-requests 40 --fetch-timeout 60 --workers 1 --receipt "$RECEIPT")"
python3 scripts/affiliate_site_evidence_card.py "$OUT"
