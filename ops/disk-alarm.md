# Root disk alarm (source only; not installed)

A systemd timer runs every 15 minutes. Its oneshot service checks `df -B1
--output=avail /` and appends one JSON observation per completed tick to
`/srv/kael-mailbox/disk-alerts.log`. Below 60 GiB it alerts Izaiah through the
existing ops-alerts sender with an explicit mention. Below 40 GiB it sets only
the existing persistent backup pause (`ops/runtime/disk-alarm/backup-paused`).
There is no build-job pause, Hermes scheduler import or Hermes-state write.
No active job is stopped; AHS units and profiles remain untouched.

The strict thresholds retain the original binary units: exactly 60 GiB is healthy;
exactly 40 GiB warns without pausing. No automatic resume exists.

## Independent review and parent-owned installation

Review `ops/systemd/kael-disk-alarm.service` and `.timer` at the exact PR head.
After separate approval, the parent installs the reviewed source and these system
units. No installation, enable/start, crontab edit or real alert is performed by
source tests. The sender `scripts/post_discord_message.py` uses stdlib only.

The original host backup cron entry was already commented out for emergency
containment. Keep it paused. Before any separately authorized resume, replace
that commented entry with the existing gated equivalent (do not leave a duplicate):

```cron
# PAUSED: 0 4 * * * /usr/bin/python3 -B /home/ubuntu/business/scripts/disk_alarm.py backup >> /home/ubuntu/business/backups/hermes-state/backup-cron.log 2>&1
```

The gate invokes the unchanged backup script with its existing lock/guards.
It refuses before alarm initialization, on its persistent pause marker, or when
admission-time free space is below 40 GiB. It starts under the alarm singleton
lock, then waits outside it, without signals or a timeout. It does not gate manual
backup invocations or an unconverted legacy cron entry. Installation alone is
not backup-resume authority.

## Existing delivery controls retained

Uses ops-alerts channel `1487300132054765629` and operator `392067741500309517`.
One warning and one critical alert per below-60 episode; healthy observations
re-arm confirmed deliveries only. A durable fsynced pending intent precedes
sender entry. A failed/ambiguous send, readback failure or crash retains pending
and suppresses new sends until the parent reconciles the exact channel effect.
Never clear pending merely to resend. The existing sender retries only explicit
HTTP429 rejection, not timeout/5xx. A pending delivery returns nonzero.

State and singleton lock stay at `ops/runtime/disk-alarm`; preserve existing
emergency holds. Natural timer execution and live Discord delivery remain
unverified until an approved installation. `check` reads df without writes/sends.

## Offline acceptance

`python3 -B -m unittest discover -s tests -p test_disk_alarm.py -v`

Checks cover thresholds, append-only observations, backup-only pause/no resume,
uncertain-send latch, pre-send intent/singleton, exact sender route and the
15-minute timer/root-filesystem command. Dedicated CI uses the exact PR head.
