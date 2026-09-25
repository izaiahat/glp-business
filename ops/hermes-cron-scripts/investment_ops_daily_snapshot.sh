#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/business/investment-ops
. .venv/bin/activate
python3 scripts/snapshot_portfolio.py --quiet
