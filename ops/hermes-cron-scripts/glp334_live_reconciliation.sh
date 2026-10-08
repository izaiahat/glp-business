#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/ubuntu/business
source_path=scripts/glp334_live_reconciliation.py
actual="$(/usr/bin/sha256sum -- "$source_path")"
[[ "${actual%% *}" == edc0de2f4f632d28a6871ffe8c271baee03f5c763a88f6aca0879e9c5b35371c ]] || { printf 'GLP334_SOURCE_DRIFT\n' >&2; exit 66; }
export PYTHONDONTWRITEBYTECODE=1
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python -B "$source_path"
