#!/usr/bin/env bash
# Daily GLP Reddit human-post pack scout (bridge-wired 2026-08-13).
# Emits one pack JSON per PT day; the canonical dispatcher cards it.
# Zero Reddit writes ever - the scout is read-only by construction.
set -euo pipefail
cd /home/ubuntu/business
SOURCE=/home/ubuntu/business/scripts/glp_reddit_daily_scout.py
SOURCE_SHA256=4f05255287f614fbb6e37eafb9a8f0bb1a3d60f83ec64b54097ae893f7f44443
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
exec python3 scripts/glp_reddit_daily_scout.py
