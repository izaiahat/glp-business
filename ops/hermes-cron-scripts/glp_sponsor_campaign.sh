#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/business
exec python3 -B scripts/glp_sponsor_campaign.py --scheduled "${@}"
