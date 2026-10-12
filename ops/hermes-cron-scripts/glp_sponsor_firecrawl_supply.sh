#!/usr/bin/env bash
# Weekday sponsor supply refresh before the 09:00 PT batch. Evidence only; never sends.
set -euo pipefail
cd /home/ubuntu/business
exec python3 -B scripts/glp_sponsor_firecrawl_supply.py daily "${@}"
