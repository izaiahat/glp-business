#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
source_path=scripts/glp334_live_reconciliation.py
actual="$(/usr/bin/sha256sum -- "$source_path")"
[[ "${actual%% *}" == f7d55f798cde54ad3289f61b38ebaccc7f2084dc5d2e9980a5690c7dfeec1c74 ]] || { printf 'GLP334_SOURCE_DRIFT\n' >&2; exit 66; }
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B "$source_path"
