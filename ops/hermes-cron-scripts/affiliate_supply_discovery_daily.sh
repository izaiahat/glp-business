#!/usr/bin/env bash
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Affiliate Monday Discovery","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"📊 **GLP Affiliate Monday Discovery** — 2 candidate(s) for review\n\njoined marketplace scans attempted: **14/14** · marketplace execution: **1013** · direct web: **Firecrawl**\nauthenticated catalogs completed: **12/14** · constrained scans: **levanta (public fallback; 0 hit(s)), sovrn (bounded attempt; 0 hit(s))**\n**1. Example GLP Clinic** · direct · $100 CPA · fit: explicit_glp1\nhttps://example.invalid/affiliate\n\nEligible candidates admitted to the existing queue: 1. Nothing submitted or deployed.","fixture_output_class":"report","fixture_test":"python3 -m pytest -q tests/test_affiliate_discovery_resume.py tests/test_affiliate_discovery_admission.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/affiliate-supply-discovery.lock
flock -n 9 || exit 0

expected_discovery_sha256='56c9a61e25e8b8ab52893e1a685cf02ae64c300aab282d21b5da0a113f1d283d'
expected_expansion_sha256='b84fd9b2dcd35fffd100229b26073fbac9a3a92eaac3cea244741e680046263c'
expected_coverage_sha256='4045c1e53c470f7540405524941263b64c9fa59136af3b5d7702e46fb0d7e39b'
expected_card_sha256='30d152f78c8729a67c148d2972ef1ba3ecebab732c01929c5313998708c394f8'
[[ "$(sha256sum scripts/affiliate_supply_discovery.py | cut -d' ' -f1)" == "$expected_discovery_sha256" ]]
[[ "$(sha256sum scripts/affiliate_supply_expansion.py | cut -d' ' -f1)" == "$expected_expansion_sha256" ]]
[[ "$(sha256sum scripts/affiliate_monday_marketplace_receipt.py | cut -d' ' -f1)" == "$expected_coverage_sha256" ]]
[[ "$(sha256sum scripts/affiliate_monday_discovery_card.py | cut -d' ' -f1)" == "$expected_card_sha256" ]]
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/monday-discovery/run-${RUN_ID}"
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
LATEST_SWEEP="$(python3 -c 'from pathlib import Path; p=sorted(Path("reports/glp-clinic-scout/affiliate-machine").glob("enumeration-*/_sweep-summary.json"), key=lambda x:x.stat().st_mtime); print(p[-1] if p else "")')"
if [[ -z "$LATEST_SWEEP" ]]; then
  printf '⚠️ **GLP Affiliate Monday Discovery** — no local marketplace receipt; coverage unknown\n'
fi
python3 scripts/affiliate_monday_marketplace_receipt.py \
  --local-summary "$LATEST_SWEEP" --output "$RUN_DIR/marketplace-coverage.json" >/dev/null
discovery_exit=0
python3 scripts/affiliate_supply_discovery.py --json --target "${AFFILIATE_MONDAY_TARGET:-10}" \
  --limit 10 --time-budget-seconds "${AFFILIATE_MONDAY_TIME_BUDGET_SECONDS:-360}" \
  --source-timeout-seconds 20 --admit-applications \
  --checkpoint reports/glp-clinic-scout/affiliate-machine/supply-discovery/checkpoint.json \
  --run-id "$RUN_ID" --output-dir "$RUN_DIR/discovery" >"$RUN_DIR/discovery-stdout.json" || discovery_exit=$?
python3 scripts/affiliate_monday_discovery_card.py --discovery-dir "$RUN_DIR/discovery" \
  --marketplace-receipt "$RUN_DIR/marketplace-coverage.json" \
  --output "$RUN_DIR/ranked-card.json"
exit "$discovery_exit"
