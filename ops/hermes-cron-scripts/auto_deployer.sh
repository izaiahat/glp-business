#!/usr/bin/env bash
# Maintained no-agent entrypoint. Install a byte-identical copy under
# ~/.hermes/scripts/ only AFTER independent exact-head acceptance and CI.
# Discord is sent once by the Python outbox; scheduler delivery MUST be local.
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR=/home/ubuntu/.hermes/cache/scratch
export PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
cd /home/ubuntu/business
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -B scripts/auto_deployer.py "$@"
