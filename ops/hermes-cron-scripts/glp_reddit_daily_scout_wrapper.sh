#!/usr/bin/env bash
# Daily GLP Reddit human-post pack scout (bridge-wired 2026-08-13).
# Emits one pack JSON per PT day; the canonical dispatcher cards it.
# Zero Reddit writes ever - the scout is read-only by construction.
set -euo pipefail

: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Reddit Scout","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"📊 **GLP Reddit Scout** — pack captured\n\nthreads: 2 · Reddit writes: 0\n-# receipts: local daily Reddit pack","fixture_output_class":"report","fixture_test":"python3 -m pytest -q tests/test_firecrawl_glp350_cron.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
cd /home/ubuntu/business
SOURCE=/home/ubuntu/business/scripts/glp_reddit_daily_scout.py
SOURCE_SHA256=5f7980f9a674b8aecdc3b894acdcf468c73e2846e59d2613302ed78ff9a47bfe
actual=$(/usr/bin/sha256sum "$SOURCE")
if [[ "${actual%% *}" != "$SOURCE_SHA256" ]]; then
  printf 'reddit_daily_scout_source_hash_mismatch\n' >&2
  exit 66
fi
if [[ "${1:-}" == "--verify-only" && "$#" == 1 ]]; then
  printf 'PASS_SOURCE_PIN_NO_EXECUTION\n'
  exit 0
fi
if [[ "$#" != 0 ]]; then
  printf 'unexpected_argv\n' >&2
  exit 64
fi
OUT="$(python3 scripts/glp_reddit_daily_scout.py)"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); print("📊 **GLP Reddit Scout** — " + str(r["status"]) + "\n\ncounts: " + json.dumps(r["counts"]) + " · Reddit writes: 0\n-# receipts: " + str(r["pack_path"]))' "$OUT"
