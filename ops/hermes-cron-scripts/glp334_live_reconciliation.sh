#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
source_path=scripts/glp334_live_reconciliation.py
actual="$(/usr/bin/sha256sum -- "$source_path")"
[[ "${actual%% *}" == 42654dfed9f75047f80e45d4df7b5e5d2c67bd2f85fabf46b73fced46130c694 ]] || { printf 'GLP334_SOURCE_DRIFT\n' >&2; exit 66; }
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B "$source_path"
