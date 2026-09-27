# MaxBounty reviewed pin adoption — preparation, not activation

`scripts/maxbounty_pin_release.py` is the narrow registered producer for the
installed capacity monitor's JSON → inline shell → paused registration closure.
It follows the Awin release's explicit exact-byte review, fsync/rename, custody
and hash-only scheduler-saver conventions; it does not invoke the Awin/fleet
scanners, generic `update_job`, provider registries, queues, browsers or secrets.
The scheduler's actual `.jobs.lock` is acquired nonblocking/fail-closed, not its
timeout-degrading generic context. The maintained `_save_jobs_unlocked` saver
writes the complete unchanged jobs list with only one scalar replaced.

## Scope and predecessor custody

- `ops/maxbounty-pin-predecessor.json` is the **unchanged installed predecessor**,
  not a generated successor to install. It has nine entries and SHA256
  `d4bdb54ffc4afa3ba3fbefd9a126688e3fff5cb1b6e5959ed802934408aedc59`.
- `ops/hermes-cron-scripts/glp_maxbounty_capacity_resume.sh` is the **unchanged
  external wrapper snapshot**, SHA256
  `b0e7d2770fcc5918aa5eaeef122bbede96caee8e68c28fa233bbcb7349215222`.
  Do not install that stale snapshot through ordinary source sync.
- Installed monitor/fresh source already matches merged #34/#40. The source
  receipt must identify a full source commit ancestral to the exact producer
  checkout and hashes for exactly `scripts/glp_maxbounty_capacity_monitor.py`
  and `scripts/glp_maxbounty_fresh_probe.py`. No source files are rewritten.
- Preserve all eight matching dependency hashes. The only manifest delta is
  the monitor hash plus fresh-producer membership. The wrapper delta is exactly
  its two inline hashes. The only scheduler delta is job `89cd7b766dba`'s
  `script_sha256`, bound to `glp_maxbounty_capacity_resume.sh` (exact basename or
  exact absolute installed path, not a same-basename path elsewhere).
- `enabled` must be false, `state` must be `paused`, no running/fire claim may
  exist, and the **entire** target record must match the receipt. Schedules,
  next-run fields, pause reason, delivery and unrelated jobs remain unchanged.

## Separate authorization before any live read or apply

This PR and tests authorize **no live adoption, scheduler read/write, portal
work, unpause or activation**. The parent/adoption owner must first admit live
registry access, obtain independent review of the producer and exact emitted
artifacts, and exclude every overlapping source/wrapper writer and direct
caller through the whole operation. Name that external exclusion evidence in
`writer_exclusion`. It is an operator admission prerequisite, **not a lock
implemented by this producer**; the scheduler lock alone cannot exclude
arbitrary file writers. Do not use lock-idle snapshots as proof of exclusion.

The review JSON schema is `maxbounty-pin-review-v1`, with fields:

- `decision`: `REVIEW_ONLY` for preview; `ACCEPT_EXACT_CURRENT_BYTES` for apply.
- `reviewer`, `reviewed_at_utc` (UTC `YYYY-MM-DDTHH:MM:SSZ`), nonempty `evidence`
  list, and `writer_exclusion` evidence reference.
- `producer_head`: full exact commit of the clean reviewed checkout. The
  executing producer's bytes must equal that commit's object.
- `source_head`: full reviewed ancestor commit containing the two source files.
- `source_sha256`: object with exactly those two relative paths and hashes.
- `predecessor_sha256`: `manifest` and `wrapper` hashes above.
- `expected_job`: the full exact record supplied by the admitted scheduler
  owner; never fill this from a historical report or invent a live record.
- `proposed_sha256`: preview's `manifest` and `wrapper` values, required for
  independent acceptance. Reviewing source alone is not artifact acceptance.

No live ACCEPT receipt is included or generated automatically. In the admitted
window, using an independently prepared receipt and exact checkout:

```sh
python3 -B /PATH/TO/EXACT-CHECKOUT/scripts/maxbounty_pin_release.py \
  --repo /PATH/TO/EXACT-CHECKOUT --review-receipt /PATH/TO/REVIEW.json
```

Preview reads only the exact closure and target scheduler store and emits both
artifact diffs and their hashes; it writes nothing and imports no scheduler.
After independent review and separate apply authority, the same command with
`--apply` binds to those proposed hashes. There are no alternate-root CLI flags,
fleet mode, baseline-acceptance mode or automatic retry. Programmatic path
relocation exists only to exercise identical logic in isolated tests.

Apply rechecks all source/dependency bytes, artifact bytes/modes, receipt bytes
and the raw registry snapshot under the scheduler lock and before each write.
It preserves private content-addressed preimages (including modes and full
registry custody), writes the two artifacts, calls the scheduler saver once,
and verifies exact jobs-list readback. Source files are never modified. A
pre-saver failure restores only producer-owned artifact bytes; unknown external
bytes are never overwritten. A possibly landed scheduler write is **not**
retried or rolled back blindly: preserve custody and reconcile with the owner.
There is no crash-atomic multi-file promise. The job stays paused across any
partial state. A used receipt intentionally refuses replay against its changed
preimage; reconcile successful readback rather than repeating apply.

## Independent post-adoption verification (separate future authority)

1. Run the installed wrapper with `--check-offline` and require
   `PASS_OFFLINE_SOURCE_PINS_NO_NETWORK`.
2. Call the installed monitor's `_fresh_source_identity(pins)` without invoking
   any lease, browser or `main`: the CLI alone does not prove fresh membership.
3. Read back the complete paused target record and compare every field except
   the authorized hash; verify all dependency pins, source heads, emitted
   manifest/wrapper hashes, custody and zero unrelated record changes.

`tests/test_maxbounty_pin_release.py` reproduces the wrapper's old refusal then
executes unchanged shell/monitor logic after producer emission with paths
relocated into an isolated fixture. It separately checks fresh membership,
full-record CAS, unknown bytes, duplicate/malformed conventions, source/head
binding, lock contention, moved inputs, rollback and uncertain saver effects.
The host-only integration invokes the actual installed scheduler saver with
`HERMES_HOME` set to a fresh fixture **before importing cron**; hosted CI skips
only that host-dependent case. Fixture receipts and dependency bytes are not
live reviews/evidence. `FRESH_VAULT_STAGES_NOT_INTEGRATED` remains a separate
runtime hold even after clean pins; this operation never enables live probing.
