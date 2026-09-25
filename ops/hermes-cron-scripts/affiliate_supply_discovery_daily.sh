#!/usr/bin/env bash
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Affiliate Monday Discovery","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"📊 **GLP Affiliate Monday Discovery** — 2 candidate(s) for review\n\njoined marketplace scans attempted: **14/14** · marketplace execution: **1013** · direct web: **Firecrawl**\nauthenticated catalogs completed: **12/14** · constrained scans: **levanta (public fallback; 0 hit(s)), sovrn (bounded attempt; 0 hit(s))**\n**1. Example GLP Clinic** · direct · $100 CPA · fit: explicit_glp1\nhttps://example.invalid/affiliate\n\nReview only. Nothing was applied, joined, deployed, or re-pointed.","fixture_output_class":"report","fixture_test":"python3 -m pytest -q tests/test_affiliate_supply_discovery.py tests/test_affiliate_monday_discovery_card.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/affiliate-supply-discovery.lock
flock -n 9 || exit 0

expected_discovery_sha256='088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf'
expected_expansion_sha256='6d583ab1523ca852c1f9342c036fac0766fe1b89819671c40b2d6612ad6fddf1'
expected_coverage_sha256='11fa4ecb791bceb70054f95a6596e691cea451fbec09d9bd58b9e9ae86e50ed6'
expected_card_sha256='0d80fe2831d41c367834e95f504edf510b2b2a1025e4e23bec94d5893d08d93f'
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
  printf '⚠️ **GLP Affiliate Monday Discovery** — no marketplace sweep receipt exists; scan refused\n'
  exit 2
fi
python3 scripts/affiliate_monday_marketplace_receipt.py \
  --local-summary "$LATEST_SWEEP" --output "$RUN_DIR/marketplace-coverage.json" >/dev/null
python3 scripts/affiliate_supply_discovery.py --json --target "${AFFILIATE_MONDAY_TARGET:-10}" \
  --limit 10 --time-budget-seconds "${AFFILIATE_MONDAY_TIME_BUDGET_SECONDS:-1500}" \
  --run-id "$RUN_ID" --output-dir "$RUN_DIR/discovery" >"$RUN_DIR/discovery-stdout.json"
python3 scripts/affiliate_monday_discovery_card.py --discovery-dir "$RUN_DIR/discovery" \
  --marketplace-receipt "$RUN_DIR/marketplace-coverage.json" \
  --output "$RUN_DIR/ranked-card.json"