#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
source_path=scripts/glp334_live_reconciliation.py
actual="$(/usr/bin/sha256sum -- "$source_path")"
[[ "${actual%% *}" == 511a6f3b2c014195e9776ded2e1451bbd11ee0d9b83e0ee55d7a26584bca7dbc ]] || { printf 'GLP334_SOURCE_DRIFT\n' >&2; exit 66; }
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B "$source_path"
