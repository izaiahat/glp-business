#!/usr/bin/env bash
set -euo pipefail
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python /home/ubuntu/business/scripts/kael_ops_standup.py --cron
