#!/usr/bin/env bash
# Weekday sponsor supply refresh before the 09:00 PT batch. Evidence only; never sends.
set -euo pipefail
cd /home/ubuntu/business
# Retained first-party research receipts feed the same guarded daily producer.
# These are research inputs, not eligible contacts or verified unsent stock.
cohort=reports/ops/fable-plan-2-20261009/lanes/SUP-STOCK
exec nice -n 19 python3 -B scripts/glp_sponsor_firecrawl_supply.py daily \
  --retained-leads "$cohort/DISCOVERY-HITS.jsonl" \
  --retained-leads "$cohort/BATCH-2/DISCOVERY-HITS.jsonl" \
  --retained-leads "$cohort/BATCH-3/DISCOVERY-HITS.jsonl" \
  --retained-leads "$cohort/BATCH-8/DISCOVERY-HITS.jsonl" "${@}"
