#!/usr/bin/env bash
# Daily GLP deploy-completeness backstop — READ-ONLY.
# Re-runs the six post-deploy completeness assertions (coverage, research
# binding, page-fact consistency, identity agreement, link-graph floor, base
# chain) against the live release, so a deploy that skipped or outran its own
# sensor cannot stay silently incomplete. Healthy = silent.
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Deploy Sensor","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **GLP Deploy Sensor** — 2 completeness checks failed on the live release\n\nrelease: **glp-stripe-webhook-restore-20260806-r1** · live programs: **29** · failures: **4**\ncoverage: program counts disagree — a filter or a stale artifact is hiding programs\nresearch_binding: mademed: research fields present on disk but missing from the content ledger\n→ repair the site data named in the sensor receipt before the next deploy\n-# receipt: /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/deploy-completeness-latest.json","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_glp_deploy_completeness_sensor.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/glp-deploy-completeness-sensor.lock
flock -n 9 || exit 0

RECEIPT=/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/deploy-completeness-latest.json
OUT=$(python3 scripts/glp_deploy_completeness_sensor.py --output "$RECEIPT" 2>/dev/null || true)

python3 - "$OUT" "$RECEIPT" <<'PY'
import json, sys
sys.path.insert(0, "/home/ubuntu/business/ops")
import discord_fmt as fmt
sys.path.insert(0, "/home/ubuntu/business/scripts")
from glp_deploy_sensor_hook import _detail_line

receipt_path = sys.argv[2]
try:
    summary = json.loads(sys.argv[1] or "{}")
except ValueError:
    summary = {}
if not summary:
    print(fmt.render(
        fmt.title("action", "GLP Deploy Sensor", "daily completeness sweep produced no result"),
        fmt.kvline(("release", "unknown"), ("failures", "unknown")),
        fmt.meaning("run the sensor by hand and inspect the private cron logs"),
        fmt.subtext(f"receipt: {receipt_path}"),
        output_class="alert"))
    sys.exit(0)

status = summary.get("status")
if status in ("PASS", "PASS_PARTIAL"):
    sys.exit(0)          # healthy = silent

try:
    full = json.load(open(receipt_path))
except (OSError, ValueError):
    full = {}
failed = summary.get("failed_checks") or full.get("failed_checks") or []
failures = full.get("failures") or summary.get("failures_sample") or []
lines = [
    fmt.title("action", "GLP Deploy Sensor",
              f"{len(failed)} completeness check(s) failed on the live release"),
    fmt.kvline(("release", summary.get("release") or full.get("release") or "unknown"),
               ("live programs", summary.get("live_programs", full.get("live_programs", "unknown"))),
               ("failures", summary.get("failure_count", len(failures)))),
]
for item in failures[:2]:
    detail = _detail_line(item)
    if detail:
        lines.append(detail)
lines.append(fmt.meaning("repair the site data named in the sensor receipt before the next deploy"))
lines.append(fmt.subtext(f"receipt: {receipt_path}"))
print(fmt.render(*lines, output_class="alert"))
PY
