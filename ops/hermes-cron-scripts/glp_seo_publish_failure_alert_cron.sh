#!/usr/bin/env bash
# Immediate operator page when a scheduled SEO publication lane fails (operator ruling 2026-08-14).
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"SEO Publish Alert","output_class":"silence","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"🔴 **SEO Publish Alert** — zero articles published today; publish chain failed\n\ncarrier: **failed** · published: **0**\n→ read the carrier receipt in #ops-alerts; bridge/autopilot own the repair","fixture_output_class":"alert","fixture_test":"python3 -c \"import ast;ast.parse(open('/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py').read())\"","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION
set -uo pipefail
umask 077
out="$(python3 /home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py 2>&1)" || {
  if [[ -v GLP320_FAILURE_SENSOR_TEST ]]; then printf 'TEST — GLP320 REFUSED (inspect report inputs/state; no replay)\n' >&2
  else printf '%s\n' "$out"; fi
  exit 1
}
if [[ -v GLP320_FAILURE_SENSOR_TEST ]]; then
  case "$out" in
    'TEST — GLP320 failure-sensor acceptance:'*) printf '%s\n' "$out" ;;
    '') ;;
    *) printf 'TEST — GLP320 REFUSED: non-TEST payload output\n' >&2; exit 1 ;;
  esac
  exit 0
fi
case "$out" in
  ALERTED*)
    st="${out#ALERTED: carrier=}"; st="${st%%,*}"
    lanes="${out#*failures=}"; lanes="${lanes%%,*}"
    printf '🔴 **SEO Publish Alert** — scheduled publication lane failed\n\ncarrier: **%s** · failed lane(s): **%s**\n→ read the lane-specific red-alert evidence in #ops-alerts; bridge/autopilot own the repair\n' "$st" "$lanes"
    ;;
  *recovery*)
    printf '🟢 **SEO Publish Alert** — recovered; scheduled publication lanes are healthy\n\nstatus: **recovered** · action: **none**\n→ no action needed\n'
    ;;
esac
exit 0
