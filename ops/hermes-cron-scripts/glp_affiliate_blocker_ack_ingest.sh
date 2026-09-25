#!/usr/bin/env bash
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Affiliate Blocker Acks","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"\u2705 **GLP Affiliate Blocker Acks** \u2014 1 operator ack(s) queued to the Sheet writer\n\n\u2192 The next /glp-sheet-sync-run writes them; nothing else is needed.\n-# registry: action_items \u00b7 queue: sheet_update_queue","fixture_output_class":"report","fixture_test":"python3 -m pytest -q tests/test_glp_application_dedupe_and_blocker_cards.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true,"format_marker":"CRON_DISCORD_HOUSE_STYLE_V1"}
# Turn operator `done AI-n` acks on affiliate application blockers into
# governed sheet_update_queue rows, then refresh the blocker registry.
#
# Output conforms to CRON_DISCORD_HOUSE_STYLE_V1 (ops/discord_fmt.py):
# title line, blank line, one `→ ` meaning line, one final `-# ` subtext.
# Runs AFTER ops/decisions-ack (cron 25 7-19/2 PT) has had a chance to resolve
# items, so a reply the operator types is on the Sheet the same day.
# Silent when nothing moved: stdout is delivered verbatim (--no-agent).
set -euo pipefail
umask 077
cd /home/ubuntu/business

out="$(/usr/bin/python3 -B scripts/glp_affiliate_blocker_action_items.py --ingest-acks 2>&1)" || {
  printf '🔴 **GLP Affiliate Blocker Acks** — the ack ingest run failed\n\n'
  printf '%s\n' "${out: -400}"
  printf -- '→ operator acks are not reaching the Sheet writer until this runs clean\n'
  printf -- '-# registry: action_items · queue: sheet_update_queue\n'
  exit 0
}
queued="$(printf '%s' "$out" | /usr/bin/python3 -c 'import sys,json;print(json.load(sys.stdin)["ack_ingest"]["queued_count"])' 2>/dev/null || echo 0)"
[ "${queued:-0}" -gt 0 ] || exit 0
printf '✅ **GLP Affiliate Blocker Acks** — %s operator ack(s) queued to the Sheet writer\n\n' "$queued"
printf '→ The next /glp-sheet-sync-run writes them; nothing else is needed.\n'
printf -- '-# registry: action_items · queue: sheet_update_queue\n'
