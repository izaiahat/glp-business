#!/usr/bin/env bash
#
# Daily snapshot of critical Hermes operator state.
#
# What gets backed up:
#   ~/.hermes/config.yaml        — operator config
#   ~/.hermes/.env               — secrets (env-style key=value)
#   ~/.hermes/auth.json          — OAuth tokens (OpenAI Codex, Nous, etc.)
#   ~/.hermes/cron/jobs.json     — hermes-managed cron schedule
#   ~/.hermes/cron/cron.db       — hermes-managed cron sqlite state
#   ~/.hermes/SOUL.md            — Hermes persona / operator doctrine
#   ~/.hermes/memories/          — Hermes memories tree (USER.md, MEMORY.md, etc.)
#
# Output:
#   /home/ubuntu/business/backups/hermes-state/hermes-state-YYYYMMDD.tar.gz
#
# Behavior:
#   - chmod 600 on the resulting tar.gz (it contains .env and auth.json)
#   - idempotent for same day (rewrites the same-day file)
#   - keeps the last 14 daily snapshots, deletes older
#   - safe to run while gateway is live (read-only on Hermes state)
#
# Cron entry (added 2026-05-27):
#   0 4 * * * bash /home/ubuntu/business/scripts/hermes-state-backup.sh >> /home/ubuntu/business/backups/hermes-state/backup-cron.log 2>&1
#
# Rationale: 2026-05-26 incident response found there was no continuous
# config backup — recoverable backups only existed because the update protocol
# and fix scripts each made ad-hoc copies. This script closes that gap.

set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BACKUP_ROOT="${HERMES_BACKUP_ROOT:-/home/ubuntu/business/backups/hermes-state}"
RETENTION_DAYS="${HERMES_BACKUP_RETENTION_DAYS:-14}"
TS_DATE="$(date -u +%Y%m%d)"
TS_FULL="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="${BACKUP_ROOT}/hermes-state-${TS_DATE}.tar.gz"
TMP_FILE="${OUT_FILE}.partial.${TS_FULL}"

mkdir -p "$BACKUP_ROOT"

log() {
  printf '%s %s\n' "[$(date -u +%FT%TZ)]" "$*"
}

log "hermes-state-backup start (out=${OUT_FILE})"

# Build list of paths to include relative to ~. Skip anything missing so the
# script is robust to partial installs.
declare -a INCLUDES=()
for rel in \
    config.yaml \
    .env \
    auth.json \
    cron/jobs.json \
    cron/cron.db \
    SOUL.md \
    memories ; do
  if [ -e "${HERMES_HOME}/${rel}" ]; then
    INCLUDES+=("${rel}")
  else
    log "skip (not present): ${rel}"
  fi
done

if [ "${#INCLUDES[@]}" -eq 0 ]; then
  log "ERROR: no Hermes state files found under ${HERMES_HOME}; aborting without writing an empty archive"
  exit 1
fi

# Tar with relative paths so restoration is path-agnostic (extract anywhere,
# then move). Capture into a tmp file first so partial writes never replace a
# good prior snapshot.
tar \
  --create \
  --gzip \
  --file "$TMP_FILE" \
  --directory "$HERMES_HOME" \
  -- "${INCLUDES[@]}"

chmod 600 "$TMP_FILE"
mv -f "$TMP_FILE" "$OUT_FILE"

SIZE_BYTES="$(stat -c%s "$OUT_FILE")"
log "wrote ${OUT_FILE} (${SIZE_BYTES} bytes, $(echo "${INCLUDES[@]}" | wc -w) paths)"

# Retention: delete snapshots older than RETENTION_DAYS days (by mtime).
# Restrict to our naming pattern so we never delete anything else operator
# may have placed in this directory.
DELETED=0
while IFS= read -r old; do
  rm -f -- "$old"
  log "pruned old snapshot: $(basename "$old")"
  DELETED=$((DELETED + 1))
done < <(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'hermes-state-????????.tar.gz' -mtime "+${RETENTION_DAYS}" -print)

# Also clean up any partial files older than 1 day (e.g. from prior runs that
# crashed between tar and mv).
while IFS= read -r partial; do
  rm -f -- "$partial"
  log "pruned stale partial: $(basename "$partial")"
done < <(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'hermes-state-*.tar.gz.partial.*' -mtime "+1" -print)

log "hermes-state-backup done (retention=${RETENTION_DAYS}d, pruned=${DELETED})"
