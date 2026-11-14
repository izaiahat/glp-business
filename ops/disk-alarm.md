# Disk alarm (source-only; parent installs)

Stdlib + one `df -B1 --output=avail` every 15 minutes. Strict thresholds:
60 GiB is healthy; below 60 warns; 40 GiB warns; below 40 additionally pauses
new scheduled backups and the identified registry main-release build-copy job.
No LLM, cleanup, service changes, signals, active-job stop, or automatic resume.
All `ahs-*` units remain untouched. Only default Hermes profile is supported.

## Identified schedules and scope

- Native ID `415ec5eba194`, script `glp_release_main_cron.sh`; read-only census
  found it already paused. The alarm checks exact ID + script under native
  `_jobs_lock(require_cross_process=True)` and uses supported `pause_job(reason=...)`.
  Existing pauses/reasons are retained. Active claims/cursors and unrelated jobs
  are not changed by the alarm; already-started work continues.
- Host backup: original command is `0 4 * * * bash /home/ubuntu/business/scripts/hermes-state-backup.sh >> /home/ubuntu/business/backups/hermes-state/backup-cron.log 2>&1`.
  It was already commented out for urgent disk containment. **Keep it paused.**
  Parent must replace its command with the gated entry below before any separately
  authorized re-enable. Do not leave an active legacy/duplicate backup line.
  The gate invokes that unchanged backup script (and its existing lock/guards).
  This avoids a recurring whole-crontab rewrite racing unrelated schedule edits.
  The alarm does not gate arbitrary manual backup invocations or unidentified jobs.

## Parent installation handoff

After independent acceptance and authorized merge, use the maintained scoped
source installer for `scripts/disk_alarm.py` (and the existing maintained
`scripts/post_discord_message.py` if not already current). The latter must support
`--ping-user` and exact readback. No child installation is authorized.

Add this exact alarm line to the existing operator user crontab:

```cron
*/15 * * * * HERMES_HOME=/home/ubuntu/.hermes /home/ubuntu/.hermes/hermes-agent/.venv/bin/python -B /home/ubuntu/business/scripts/disk_alarm.py tick >> /home/ubuntu/business/ops/runtime/disk-alarm.log 2>&1
```

Ensure `/home/ubuntu/business/ops/runtime` exists before cron redirects the log.
Replace the **commented** backup line with this **commented** gated equivalent;
remove the comment only under separate backup-resume authority:

```cron
# PAUSED: 0 4 * * * HERMES_HOME=/home/ubuntu/.hermes /home/ubuntu/.hermes/hermes-agent/.venv/bin/python -B /home/ubuntu/business/scripts/disk_alarm.py backup >> /home/ubuntu/business/backups/hermes-state/backup-cron.log 2>&1
```

`check` is the no-write/no-send read-only command. `tick` is effectful and may
send immediately below 60 GiB. No initial manual tick is implied by source tests.
`backup` refuses before alarm initialization, on its persistent pause marker,
or creates that marker and refuses if the admission-time df is below 40 GiB.
It launches under the alarm singleton lock, then waits outside that lock, without
signals or a timeout; the backup's own existing singleton remains authoritative.

## Notification and assistant observation

Uses only existing ops-alerts channel `1487300132054765629`, explicitly mentions
operator `392067741500309517` through the existing sender's single-user allowlist.
Message addresses operator + assistant; this does not create/wake an LLM session.
Assistant can read the existing channel or `ops/runtime/disk-alarm/state.json`.
One warning and one critical alert per below-60 episode; a healthy observation
re-arms only **confirmed** deliveries. Critical effects are rechecked every tick.
A durable fsynced pending intent precedes sender entry. Any failed/ambiguous send,
readback failure, or crash retains pending across recovery and suppresses ALL new
sends until the parent reconciles the exact channel effect. Never delete/reset
pending merely to obtain another alert. Existing sender retries only explicit
HTTP429 rejections, never timeout/5xx. Pause failure still produces an alert;
continued pending or pause error is reflected in state/stdout and nonzero exit.

Recovery never resumes anything. Parent must inspect pending delivery, current
free space and existing pause ownership before a separately approved manual
native resume or removal of `ops/runtime/disk-alarm/backup-paused` under `.lock`.
Preserve existing emergency holds. Protect runtime state from cleanup; it is not
curated source. Natural cron + live Discord delivery remain parent verification.

## Owning checks

```sh
python3 -B -m unittest discover -s tests -p test_disk_alarm.py -v
```

Offline fixtures cover exact 60/40 byte boundaries, persistent backup refusal,
no auto-resume, exact native target with active-claim/unrelated-job preservation,
singleton contention, durable pre-send crash/timeout latch and fixed sender route.
Real df and CLI help are read-only. Hosted `disk-alarm-source` binds exact PR head.
