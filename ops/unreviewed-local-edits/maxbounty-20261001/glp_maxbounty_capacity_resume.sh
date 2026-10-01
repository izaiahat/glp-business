#!/usr/bin/env bash
set -euo pipefail
# CRON_DISCORD_HOUSE_STYLE_V1
# Read-only same-job replacement. No replay, submission or Sheet path.
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"MaxBounty Capacity","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","route":"1487300132054765629","success_silence":true,"detail_policy":"complete_summary_in_discord","fixture":"📊 **MaxBounty Capacity** — read-only observation\n\nProvider capacity: **BLOCKED**; pending-request count: **UNKNOWN**.\n→ no applications, queue or Sheet changes\n-# state: /home/ubuntu/business/ops/state/glp-maxbounty-capacity-monitor.json","fixture_output_class":"report","fixture_test":"test_monitor.py::test_durable_silent_unchanged_unknown_not_zero"}
unset DEBUG PWDEBUG PLAYWRIGHT_DEBUG NODE_DEBUG
PYTHON=/home/ubuntu/.hermes/venvs/glp-awin-runtime-r2/bin/python
SCRIPT="/home/ubuntu/business/scripts/glp_maxbounty_capacity_monitor.py"
PINS="/home/ubuntu/business/scripts/glp_maxbounty_capacity_monitor.pins.json"
[[ -x "$PYTHON" && -f "$SCRIPT" && -f "$PINS" ]] || { printf '%s\n' 'MaxBounty monitor: source/runtime missing'; exit 2; }
[[ "$(sha256sum "$SCRIPT" | cut -d' ' -f1)" == "a80092bb830a68931383f5819ecb81b0fc726ff69d4583cb02d7d3bf63c22f73" && "$(sha256sum "$PINS" | cut -d' ' -f1)" == "114db70487e59a819e7913f12698176775d6d97a6340c801794c0f01cec3a920" ]] || { printf '%s\n' 'MaxBounty monitor: reviewed source pin mismatch'; exit 2; }
exec "$PYTHON" -I -B "$SCRIPT" --pins "$PINS" "$@"
