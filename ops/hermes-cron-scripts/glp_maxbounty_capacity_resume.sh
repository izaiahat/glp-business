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
[[ "$(sha256sum "$SCRIPT" | cut -d' ' -f1)" == "441e990d098bfea79485a6e01b713948ca3aee1b15c7140d55fc2c18e0f83da4" && "$(sha256sum "$PINS" | cut -d' ' -f1)" == "d4bdb54ffc4afa3ba3fbefd9a126688e3fff5cb1b6e5959ed802934408aedc59" ]] || { printf '%s\n' 'MaxBounty monitor: reviewed source pin mismatch'; exit 2; }
exec "$PYTHON" -I -B "$SCRIPT" --pins "$PINS" "$@"
