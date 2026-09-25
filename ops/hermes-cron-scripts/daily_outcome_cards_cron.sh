#!/usr/bin/env bash
# One GLP-only outcome card; cumulative records are not business effects.
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Daily Outcomes","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"📊 **Daily Outcomes** — GLP-only outcome report\n\nnew routes: **1** · R2 refreshes: **UNKNOWN** · applications verified records: **14** cumulative\n→ inspect named run gaps; records and runtime success are not sales","fixture_output_class":"report","fixture_test":"python3 -c \"import ast;ast.parse(open('/home/ubuntu/.hermes/scripts/daily_outcome_cards.py').read())\"","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
CRON_DECLARATION
set -uo pipefail
umask 077
# GLP-228 (2026-09-13): payload sha pin REMOVED. This wrapper is named in the
# 2026-08-20 internal-cron governance strip (tests/test_internal_cron_governance_strip.py):
# an internal report lane's payload may change under its own test suite without
# re-pinning the wrapper. An identical pin on glp_affiliate_approval_reconciler.sh
# killed that job silently for ~25h on 2026-09-12 the moment its payload legitimately
# changed. This pin matched today only by luck of nothing having edited the composer yet.
# Wrappers with EXTERNAL effects stay pinned on purpose and are untouched:
# glp_awin_drain_sheet_queue.sh (Sheet writes), awin_auth_liveness_preflight.sh (provider identity).
python3 /home/ubuntu/.hermes/scripts/daily_outcome_cards.py
