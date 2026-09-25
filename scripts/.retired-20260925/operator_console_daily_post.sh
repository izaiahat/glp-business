#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/business
LOG=/home/ubuntu/business/logs/operator-console-daily-post.log
mkdir -p /home/ubuntu/business/logs
{
  echo "=== operator console daily post $(date -Is) ==="
  /usr/bin/python3 /home/ubuntu/business/ops/operator-console/operator_console.py build
  OPERATOR_CONSOLE_SCHEDULED_POST=1 \
    /usr/bin/python3 /home/ubuntu/business/ops/operator-console/operator_console.py post
} >> "$LOG" 2>&1
