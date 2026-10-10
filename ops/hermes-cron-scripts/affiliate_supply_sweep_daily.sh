#!/usr/bin/env bash
# Daily affiliate SUPPLY SWEEP (phase 0 of the machine — runs before the backrun).
#
# This job was born from the 2026-08-06 verification finding that the
# application pool was half-enumerated:
#
#   1. FULL-CATALOG SWEEP. Every credentialed network, every relationship
#      bucket, paginated to the provider's own reported total, with a
#      COMPLETENESS verdict per network. Before this existed, CJ returned 8 of
#      2,868 advertisers, Rakuten 0 of 2,172 and Katalys 93 of 405 — all three
#      reporting PASS, because "the call succeeded" and "the catalog is
#      enumerated" were the same signal.
# Site evidence is a separate hash-pinned scheduled stage after this sweep. The
# split prevents duplicate Firecrawl spend and lets the scheduler prove each
# machine boundary independently.
#
# Output lands in enumeration-<stamp>/, which the backrun's ingest and the
# auto-apply reader already glob — so both pick up the complete inventory
# without learning a new path.
#
# Read-only: GETs against provider business state plus OAuth token mints.
# Submits no applications, writes nothing to the Sheet, clicks no tracking
# links, prints no secrets.
#
# Speaks ONLY when a network's catalog came back INCOMPLETE or BLOCKED — that
# is supply the machine cannot see and an operator can act on. Silent on a
# clean sweep.
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Affiliate Supply Sweep","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **Affiliate Supply Sweep** — 2 of 10 marketplace catalogs came back incomplete\n\ncatalog rows enumerated: **26,603** · site evidence harvested: **0**\n**rakuten** — only 2000 of 2172 rows enumerated\n**katalys** — provider returned an error and no rows\n→ these programs are invisible to the application lane until the sweep completes\n-# receipts: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_marketplace_supply_sweep.py tests/test_affiliate_site_evidence_lane.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
cd /home/ubuntu/business

exec 9>/home/ubuntu/.hermes/state/affiliate-supply-sweep.lock
flock -n 9 || exit 0

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="reports/glp-clinic-scout/affiliate-machine/enumeration-${STAMP}"
# The affiliate-blitz venv carries playwright + requests; the hermes venv does
# not (2026-08-06 root cause of the silent CJ readback failure).
PY=/home/ubuntu/business/.venv-affiliate-blitz/bin/python
[[ "$(sha256sum scripts/affiliate_catalog_batch.py | cut -d' ' -f1)" == '5d54653462b04c9c9f1ffa923ad9dc79bea49308be45c69c59f9be84255410a3' ]]
[[ "${AFFILIATE_CRON_VERIFY_ONLY:-0}" == "1" ]] && exit 0

set +e
SWEEP=$("$PY" scripts/affiliate_catalog_batch.py --outdir "$OUTDIR" 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  failure_tail="$SWEEP"
  if [[ ${#failure_tail} -gt 2000 ]]; then failure_tail="${failure_tail: -2000}"; fi
  printf '⚠️ **Affiliate Supply Sweep** — sweep crashed (rc %s)\n\n%s\n' "$rc" "$failure_tail"
  exit "$rc"
fi
python3 scripts/affiliate_supply_sweep_card.py "$SWEEP"
