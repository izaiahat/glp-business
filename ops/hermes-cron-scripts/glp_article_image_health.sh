#!/usr/bin/env bash
# GLP article-image lane degradation sensor.
# Silent on healthy/transient; posts ONE #decisions card (via ops/action_items)
# only when the lane has degraded twice in a row for a cause a machine cannot
# clear itself (credits, credentials, missing CLI). Stdout stays empty so the
# scheduler never duplicates the card.
set -uo pipefail
err="$(mktemp)"
trap 'rm -f -- "$err"' EXIT
/usr/bin/python3.12 /home/ubuntu/business/systems/seo-blog-machine/scripts/article_image_health_alert.py --surface 1 >/dev/null 2>"$err"
rc=$?
if [[ -s "$err" ]]; then
  printf 'GLP article-image health stderr (rc=%s):\n' "$rc" >&2
  while IFS= read -r line || [[ -n "$line" ]]; do printf '%s\n' "$line" >&2; done < "$err"
fi
exit "$rc"
