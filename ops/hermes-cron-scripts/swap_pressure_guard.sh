#!/usr/bin/env bash
# Alert before swap pressure becomes a livelock; never mutate swap or kill work.
#
# The guard distinguishes cold long-lived heap from active pressure and also
# catches a single runaway RSS process before the 92% CloudWatch alarm. It emits
# only process ID/name/size, never argv or environment content. Remediation stays
# manual because an automated kill cannot prove that a process is not publishing.
#
# Silent when healthy; one card per pressure class/process until recovery.
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Swap Guard","output_class":"silence","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"⚠️ **Swap Guard** — swap 94% with only 3.1GB available\n\nreclaim withheld: RAM is not abundant enough to fault pages back safely\n→ this is real memory pressure; inspect the top RSS processes before any restart\n-# host: hermes-01 · no system state was changed","fixture_output_class":"alert","fixture_test":"bash /home/ubuntu/.hermes/scripts/swap_pressure_guard.sh --self-test","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
CRON_DECLARATION

HIGH_WATER_PCT=75          # early warning, well before CloudWatch's 92%
SAFETY_MULTIPLE=2          # classify active pressure vs cold swap
RUNAWAY_RSS_KB=$(( 4 * 1024 * 1024 ))
STATE_FILE='/home/ubuntu/.hermes/state/swap-guard-state'

read_kb() { awk -v k="$1" '$1==k":"{print $2}' /proc/meminfo; }

swap_total=$(read_kb SwapTotal)
swap_free=$(read_kb SwapFree)
mem_avail=$(read_kb MemAvailable)
[[ "${swap_total:-0}" -gt 0 ]] || exit 0
swap_used=$(( swap_total - swap_free ))
swap_pct=$(( swap_used * 100 / swap_total ))

gb() { awk -v k="$1" 'BEGIN{printf "%.1f", k/1048576}'; }

top_rss_process() {
  local status_file pid rss name best_pid=0 best_rss=0 best_name=unknown
  for status_file in /proc/[0-9]*/status; do
    [[ -r "$status_file" ]] || continue
    pid=${status_file#/proc/}; pid=${pid%/status}
    rss=$(awk '$1=="VmRSS:"{print $2; exit}' "$status_file" 2>/dev/null || true)
    [[ "${rss:-}" =~ ^[0-9]+$ ]] || continue
    if [[ "$rss" -gt "$best_rss" ]]; then
      best_pid=$pid
      best_rss=$rss
      if [[ -r "/proc/$pid/comm" ]]; then
        IFS= read -r name < "/proc/$pid/comm" || name=unknown
        best_name=${name:-unknown}
      else
        best_name=unknown
      fi
    fi
  done
  printf '%s|%s|%s\n' "$best_pid" "$best_name" "$best_rss"
}

decision_for() { # swap_pct mem_available_kb swap_used_kb top_rss_kb
  if [[ "$4" -ge "$RUNAWAY_RSS_KB" ]]; then
    printf 'runaway\n'
  elif [[ "$1" -lt "$HIGH_WATER_PCT" ]]; then
    printf 'silent\n'
  elif [[ "$2" -lt $(( "$3" * SAFETY_MULTIPLE )) ]]; then
    printf 'pressure\n'
  else
    printf 'cold_swap\n'
  fi
}

if [[ "${1:-}" == '--self-test' ]]; then
  # Prove the decision table without touching system state.
  fail=0
  check() { # pct avail_kb used_kb rss_kb expected
    local p=$1 a=$2 u=$3 r=$4 want=$5 got
    got=$(decision_for "$p" "$a" "$u" "$r")
    [[ "$got" == "$want" ]] || { printf 'FAIL pct=%s avail=%s used=%s rss=%s want=%s got=%s\n' "$p" "$a" "$u" "$r" "$want" "$got"; fail=1; }
  }
  check 40 27000000 3000000 1000000 silent
  check 40 27000000 3000000 5000000 runaway
  check 76  3000000 8000000 1000000 pressure
  check 76 27000000 8000000 1000000 cold_swap
  check 75 16000000 8000000 1000000 cold_swap
  [[ $fail -eq 0 ]] && echo 'swap guard self-test PASS' || exit 1
  exit 0
fi

IFS='|' read -r top_pid top_name top_rss_kb <<< "$(top_rss_process)"
decision=$(decision_for "$swap_pct" "$mem_avail" "$swap_used" "$top_rss_kb")

if [[ "$decision" == 'silent' ]]; then
  rm -f "$STATE_FILE" 2>/dev/null || true   # recovered; next high event may alert again
  exit 0
fi

if [[ "$decision" == 'runaway' ]]; then
  sig="runaway:${top_pid}"
  [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE" 2>/dev/null)" == "$sig" ]] && exit 0
  printf '⚠️ **Swap Guard** — early runaway process pressure\n\nPID **%s** · process **%s** · RSS **%sGB** · swap **%s%%**\n→ inspect ancestry and prove the process is not mid publish/deploy before terminating it; the guard never kills work\n-# host: hermes-01 · 4.0GB RSS early threshold · no system state changed\n' \
    "$top_pid" "$top_name" "$(gb "$top_rss_kb")" "$swap_pct"
  printf '%s' "$sig" > "$STATE_FILE"; chmod 600 "$STATE_FILE" 2>/dev/null || true
  exit 0
fi

if [[ "$decision" == 'pressure' ]]; then
  sig="pressure:${top_pid}"
  [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE" 2>/dev/null)" == "$sig" ]] && exit 0
  printf '⚠️ **Swap Guard** — swap %s%% with only %sGB available\n\ntop RSS: PID **%s** · process **%s** · **%sGB**\nreclaim withheld: RAM is not abundant enough to fault pages back safely\n→ this is real memory pressure; inspect the top RSS process before any restart\n-# host: hermes-01 · no system state changed\n' \
    "$swap_pct" "$(gb "$mem_avail")" "$top_pid" "$top_name" "$(gb "$top_rss_kb")"
  printf '%s' "$sig" > "$STATE_FILE"; chmod 600 "$STATE_FILE" 2>/dev/null || true
  exit 0
fi

# Cold live heap is alert-only. swapoff previously caused heavy EBS I/O and the
# kernel evicted the same pages again; the guard must not repeat that cycle.
sig='cold_swap'
[[ -f "$STATE_FILE" && "$(cat "$STATE_FILE" 2>/dev/null)" == "$sig" ]] && exit 0
printf '⚠️ **Swap Guard** — swap %s%% full (%sGB RAM available)\n\ntop RSS: PID **%s** · process **%s** · **%sGB**\nreclaim intentionally NOT run: cold live heap cycles back into swap and stalls I O\n→ inspect long lived processes in a proven quiet window; no automatic restart or swap mutation\n-# host: hermes-01 · 75%% early threshold\n' \
  "$swap_pct" "$(gb "$mem_avail")" "$top_pid" "$top_name" "$(gb "$top_rss_kb")"
printf '%s' "$sig" > "$STATE_FILE"; chmod 600 "$STATE_FILE" 2>/dev/null || true
