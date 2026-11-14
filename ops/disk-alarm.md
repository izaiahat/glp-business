# Root disk alarm (source only; not installed)

A systemd timer checks `df -B1 --output=avail /` every 15 minutes. Each tick
appends a JSON observation to `/srv/kael-mailbox/disk-alerts.log`. Below 60 GiB
it alerts Izaiah through the existing ops-alerts sender with an explicit mention.
Below 40 GiB it sets `ops/runtime/disk-alarm/backup-paused`, tagged
`owner: disk-alarm`. The next healthy tick (at least 60 GiB) removes only that
alarm-owned marker and logs `backup_recovery: alarm hold lifted`.
Exactly 40 GiB warns without pausing. A warning does not clear a prior pause.

Separate `operator-backup-paused` markers, unknown/legacy `backup-paused`
contents and the existing commented cron hold are never cleared automatically.
There is no build-job pause, Hermes scheduler import or Hermes-state write by
the alarm. No active job is stopped; AHS units and profiles remain untouched.

## Independent review and parent-owned installation

Review the complete source delta, including `scripts/hermes-state-backup.sh`,
at the exact PR head. After separate approval the parent installs these sources
and the service/timer. No installation, enable/start, crontab edit or real alert
is performed by source tests. The sender uses stdlib only.

Read-only host inspection for request claude-orch-20261114-008 confirmed the
original 04:00 backup entry remains commented for operator disk containment.
Keep it commented until separately authorized to resume. No cron migration is
needed: the backup script now calls `disk_alarm.py admit-backup` before any
backup directory, lock, staging, publication or retention operation. This covers
both the legacy direct cron command and manual invocation. Install the alarm
module and changed backup entry together; a missing module refuses admission.
Installation is not backup-resume authority.

Admission takes the alarm singleton lock, validates the state and its latest
observation (no more than 30 minutes old, no monitoring error), checks fresh df,
and refuses below 40 GiB or on either pause marker. Busy/paused admission returns
3 without starting a backup; measurement/state errors also return nonzero. The optional
`disk_alarm.py backup` wrapper remains supported. The lock covers admission/start,
not the duration of a backup; existing backup locks/guards remain unchanged.
The wrapper admits once under the lock and writes a random single-use token in
the owner-only mode-0700 `admissions` directory. Its child receives the token via
`DISK_ALARM_BACKUP_TOKEN`; the gate atomically unlinks its mode-0600 proof file,
checks the matching token and age under 60 seconds, then skips lock re-entry.
Absent, invalid, expired or consumed proof falls back to normal locked admission.
The old `DISK_ALARM_BACKUP_ADMITTED=1` flag grants no admission.
No signal or timeout is added to an active backup. A later pause affects the next
admission, not in-flight work.

## Monitoring failures

A failed, timed-out or malformed df is UNKNOWN, logged and alerted as ERROR,
and creates an alarm-owned pause. Validate the exact C-locale header, unsigned
ASCII bytes and available <= root filesystem size; do not accept arbitrary
headers or implausible positives as healthy.

Corrupt primary state is not overwritten. Preserve its uncertain deliveries;
log/alert the error using a separate `monitoring-error.json` intent and keep
backup admission closed. The operator must reconcile/repair the exact primary
state; a subsequent healthy tick can lift the alarm hold. Do not delete or reset
state just to unblock admission. Legacy state without `latest` is read as having
no observation yet; admission refuses until a tick records a valid observation.
Healthy ticks clear the sidecar's confirmed sent levels but preserve pending
intents and reconciliation evidence, so a later corruption alerts again.
Failed filesystem writes prevent sending;
stderr/journald records the failure when the normal log is unavailable, with a
best-effort pause. No software can promise a durable marker/log on an unwritable
filesystem; admission always re-measures and refuses below 40 GiB or if df fails,
even when cached state is healthy and neither state nor pause writes succeeded.
Below the 60 GiB resume line, admission must also persist its own observation;
a failed write refuses, including at 40–60 GiB without a recorded pause. This
observation never refreshes the timer's `latest`: the last persisted timer tick
must still be no more than two 15-minute intervals old.

## Delivery and exact-effect reconciliation

Uses ops-alerts channel `1487300132054765629` and operator `392067741500309517`.
One confirmed alert per severity per below-60 episode; healthy observations
re-arm confirmed deliveries. Pending warning, critical and monitoring-error
intents latch independently: an uncertain warning never suppresses a new critical
intent, but the warning itself is never replayed. Predecessor singleton intents
are preserved with a deterministic reconciliation ID. An unresolved primary
intent returns nonzero. Corrupt-state error intents live in `monitoring-error.json`
and must also be reconciled, even after the primary state is repaired.

Intent and observation are fsynced before log/sender entry. Crash, timeout,
readback failure and ambiguous sender outcome retain the exact pending ID.
The sender retries only explicit HTTP429 rejection, not timeout/5xx.

Reconciliation is operator-owned, under the same singleton lock. Read the exact
pending `id`, `level`, `message`, and `at` from the relevant state file. Inspect
the exact channel/sender receipt. A missing search result or timeout is not proof
of no send: leave it pending until resolved. For confirmed delivery, record:

    python3 -B scripts/disk_alarm.py reconcile --level warning --intent-id EXACT_ID --outcome delivered --message-id EXACT_MESSAGE_ID --evidence EXACT_READBACK_URL

Use `--state-file monitoring-error.json --level error` for a corrupt-state alert.
Only with positive proof of rejection/no sender entry, record `--outcome not-sent`
and its evidence instead. Both dispositions retain the original intent and evidence
in `reconciled`; neither sends. `delivered` marks that severity sent; `not-sent`
allows the next tick to attempt that severity only if still warranted. Wrong intent
IDs refuse, as does delivered without a message ID. Never clear pending manually.

## Offline acceptance

`python3 -B -m unittest discover -s tests -p test_disk_alarm.py -v`

Covers thresholds, logged recovery/operator holds, direct nightly-script refusal
and a real scratch-only backup after recovery, failed measurements, corrupt/stale
state, failed writes, independent delivery latches/reconciliation, crash intent,
singleton, sender route and real read-only df. Dedicated CI checks out the exact
PR head. Natural scheduling, live alert delivery and installed behavior remain
unverified; `check` reads df without writes/sends.
