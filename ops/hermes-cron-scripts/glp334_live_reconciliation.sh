#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
source_path=scripts/glp334_live_reconciliation.py
actual="$(/usr/bin/sha256sum -- "$source_path")"
[[ "${actual%% *}" == cfb1526e60463dfdcc87d0e6a138d54c4f71cdd7854b7cfaf70e138afd27ffaf ]] || { printf 'GLP334_SOURCE_DRIFT\n' >&2; exit 66; }
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B "$source_path"
