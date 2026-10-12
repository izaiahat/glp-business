#!/usr/bin/env bash
# FC71 scheduled Firecrawl job: provider_research. Evidence files only; per-day cap and
# 3000-credit floor enforced in scripts/glp_firecrawl_jobs.py before each call.
set -euo pipefail
umask 077
cd /home/ubuntu/business
exec 9>/home/ubuntu/.hermes/state/glp-firecrawl-provider_research.lock
flock -n 9 || exit 0
exec python3 -B scripts/glp_firecrawl_jobs.py provider_research "$@"
