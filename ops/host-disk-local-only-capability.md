# GLP349 registered local-only capability (source only)

## Capability and selection

The existing two registered wrappers (`075ed81c6b43` safe-pruner and
`47a453ceded2` low-free) launch **the same** `host-disk-cleanup.service` through
`scripts/host_disk_pruner_trigger.py:trigger()`. Do not register another job or
unit, submit a side request, set manager-wide environment, or call the raw pruner
as a substitute. `--dry-run` on the cron wrapper remains legacy-only dry-run.

New source capability: the controller's explicit `--local-only` propagates to the
large sweep's explicit `--local-only`. Neither flag existed in PR531. The fixed
unit ExecStart is the selection boundary, not credentials or a mutable request
file. `ops/systemd/host-disk-cleanup-local-only.conf.example` is reviewed
alternative content for the **existing** `large-classes.conf` drop-in; it is not
installed or enabled by this PR. Without that explicit selection the old full
mode remains the default. Both registered wrappers then enter the selected mode
without scheduler arguments or a new dispatch path. A request made while an old
full-mode invocation is active still skips the duplicate; it does not become a
local-only invocation. No hot mode switch is supported.

In local-only mode the sweep never constructs `host_disk_r2.Destination`, reads
its credentials, calls `Control`, performs archive migration or requests a remote
summary. Local archive calls get `archive_destination=None`, selecting the
existing local-verified custody path; ordinary local node/scratch/duplicate,
completed dependency, site/archive and legacy safe + D/A/B/C classes still run
under their unchanged reference/age/receipt/financial/privacy/headroom guards.
R2 migration and the remote-only 30-day/1MiB report phase are omitted, not
relabeled as successful local work or converted to a new local-retirement class.
The existing local 7-day/128MiB report threshold remains unchanged. Terminal sweep
and controller receipts record `local_only`; R2 status is
`DISABLED_LOCAL_ONLY`. Local-only is **not** a no-send mode: the unchanged
controller's existing completion notification remains part of the chain.

## Coupled source / receiving review cohort

- `scripts/host_disk_cleanup_large_unit.py`: argument, sweep propagation, receipt
  mode, candidate-relative imports (same installed origin when received).
- `scripts/host_disk_large_sweep.py`: capability exclusion only; default path and
  policy scopes unchanged. No R2 module or deletion-class changes.
- `ops/systemd/host-disk-cleanup-local-only.conf.example`: opt-in fixed ExecStart
  for the existing unit and unchanged proof hooks. Parent must authenticate the
  effective loaded ExecStart, not just the example file.
- `ops/hermes-cron-scripts/host_disk_pruner.py`: restore the authentic trigger
  execution body from the installed/registered baseline SHA256
  `e9847e3fd7a3e9ae27bcf891b09e2219045d7b87ad39b57f703119b1cda76c72`.
  The output fixture now uses mode-neutral wording, so the successor requires
  explicit `075ed81c6b43` native wrapper SHA repinning at authorized receiving.
- `scripts/host_disk_pruner_trigger.py`: curate authentic installed launch logic,
  with mode-neutral output (not an unsupported full-mode claim). Low-free wrapper
  is unchanged, registered SHA256
  `0fd59dad6dcf953f836e85a904a0406ef9216fb1749b4a40decd79dc2add2b8b`.
- Source manifest leaves, source-drift wrapper expectation and direct trigger /
  controller / sweep payload pins, explicit allowlist, focused tests and their
  existing exact-head CI step are reviewed together. These are source metadata,
  **not** installed sync baselines or native job-pin writes. No global manifest
  adoption is authorized.
- `tests/test_scheduled_source_drift.py`: extend the existing immutable-preimage
  full-document transition oracle with exactly this host-storage row's wrapper
  hash and three payload pins. Prior historical assertions and full equality
  remain intact; no arbitrary inventory drift is accepted.

Receiving/adoption is parent-owned and requires independent exact-head review,
normal existing source/cleanup/native singleton custody and no active old
controller/readers. Review source/wrapper/unit and native pin cohort together;
never install a controller/sweep partially or change the drop-in while a full
invocation is active. No install, daemon-reload, trigger, qualification, policy
pointer, credential/environment manipulation, upload or reclaim is performed
here. Historical qualification receipt `c1a21e627ae455e071fa32afcb08b051d1e8f1f78ae01f5519f52c5b5ea6d44b`
and policy remain untouched. Parent consumed its sole natural observer: the
bound PID 3282561 / invocation 042f7501c43545d88ad14d930aac3502 ended 11:17:23Z,
ExecMainStatus 1 / completed_with_errors, authentic completion SHA256
`ff989a803a28e6cde3a2c1e37d5df3b96d3991bf04f54158183b9ce6247a53f2`.
Parent recomputed every row/rule total as zero. C's 160 candidates include 159
snapshot refusals naming route/queue, not proof of 159 writes or an actual writer;
one cites incomplete relay/lane/Linear refresh. D remained stale, A time-budget
partial, B zero links. This does not assert a present lock-free window; later
native invocations may exist. Observer/receipt namespace is not read or modified
by this source work.

## Queue-reference churn: maintained-source trace, not PID attribution

The actual refusal names `ops/gpt6pro-relay/route/queue`. This proves a directory
identity changed during collection, **not** who wrote it. No live queue scan or
collector rerun was used.

Maintained writer definition: `scripts/pro_route.py:save()` calls
`gpt6pro_relay._atomic_json(directory()/'queue'/(row['job']+'.json'), row)`.
`_atomic_json` creates `<row>.json.<pid>.tmp`, flushes/fsyncs, then `os.replace`s
and fsyncs the parent. Each save can change directory mtime/ctime and row inode,
including logically unchanged complete/uncertain rows in a dispatch tick.

All in-module queue-save paths hold the existing `pro_route.singleton()`:
`enqueue`, `enqueue_replacement` (both successor and predecessor), `assign`,
`requeue_unsent`, backlog indexing in `ingest_backlog`, `dispatch`, and CLI
`follow-up`. `singleton()` opens `route/dispatcher.lock` in append mode and takes
`flock(LOCK_EX|LOCK_NB)`, releases by closing at context exit. `save()` itself
has no lock assertion; arbitrary external direct callers are noncooperative.
`consume_outboxes` uses **consumer.lock** for delivery receipts/copies and reads
queue rows; it is not the queue writer merely because it shares the directory.

`dispatch` holds dispatcher.lock across `effect()` (heavy/account/transport
admission), all saves and poll stagger sleeps. `effect` obtains the host heavy
seat via `candidate_build_lock.py`, then relay account/transport locks. The
collector walks every relay directory but reads JOB/ledger sources and their
transitive proofs, then lane results and live Linear, then revalidates **all**
identities. Queue row JSONs are not special-cased out; their directory identities
remain bound. `ReferenceGuardAPI` and ordinary C consumers revalidate that
admitted identity set again at effects. Locking only collection and releasing
before effects may eliminate the initial race but cannot make later churn safe.

### Existing dispatcher exclusion in the explicit local-only controller

The registered controller now opens the **existing** `route/dispatcher.lock`
read-only, no-follow, close-on-exec, verifies a regular file and the same named
inode, then takes the same `flock(LOCK_EX|LOCK_NB)` protocol as the dispatcher.
It neither constructs a route/lock nor imports the relay module. Missing,
unreadable, changed or busy custody returns **75 / deferred before cleanup**:
no cleanup lock, report, reference capture, local retirement or R2 operation is
started. This is not completed cleanup or a zero-reclaim success.

The EX lease is acquired **before** the cleanup singleton and any source lease,
kept by the controller across reference capture, all protected final checks and
local retirement in both the large sweep and legacy D/A/B/C subprocesses, and
closed in `finally` on ordinary return or exception. It is not passed to child
processes or retained by a proof observer. The existing phase budgets remain
900s large sweep (2700s under existing pressure selection), 300s safe rules and
1050s D/A/B/C; external-pin calls and
completion delivery retain their existing limits. No deadline, age, threshold or
qualification budget is extended. The default full controller and other jobs do
not acquire this exclusion; invoking the raw sweep is not a registered selector.

Lock-order trace: the native Pro wrapper validates its source pins then `exec`s
`pro_route.py dispatch` without a cleanup/source/heavy lease. `dispatch` acquires
dispatcher then `effect` acquires heavy then relay account/transport custody.
The local-only controller acquires dispatcher nonblocking first, cleanup second;
its evidence child later acquires the existing source lease in each D/A/B/C
scope. Neither cleanup nor source custody is held while attempting dispatcher.
No maintained queue writer was found with an outer cleanup/source lease, and
no cleanup call to Pro's sender/consumer is introduced.

Tradeoff: separately admitted local cleanup can pace **this dispatcher only**
for the unchanged finite controller run. An already-owned dispatcher is never
waited for, killed, paused, signalled or reclaimed; the cleanup defers. This is
not authority to freeze arbitrary agents or to change Pro sender/consumer
policy. Remote generating chats and UNKNOWN downloads remain untouched. The
mutex coordinates cooperative queue writers only: all existing complete
namespace discovery, mtime/ctime, directory/file identities and final checks
remain mandatory. Noncooperative queue writes and unrelated/Linear churn still
refuse. No new store, stable-generation API, lock framework or queue exception
is needed or added.

## Focused acceptance

`tests/test_host_disk_local_only.py` contains one registered-wrapper → actual
trigger → fixture systemd selected-unit → actual controller → actual sweep
check with all R2 boundaries counted/forbidden, a real private scratch archive
compressed/verified/retired under unchanged local guards, and fixture-only
legacy subprocess receipts/sends. The same chain uses a real separate-process
contender running the maintained `pro_route.singleton()` body on one private
lock inode: it is excluded at complete reference capture, local retirement and
legacy final-phase admission, then admitted after ordinary or exceptional exit.
A busy-owner CLI check proves exit75 before cleanup/source custody, local/R2
effects or success receipts; no owner is disturbed. These are **not** native
scheduler/runtime proof. The retained uncooperative private queue publication
still invokes the complete collector's identity refusal and zero-effect receipt.
Default destination construction and delegated-trigger source-drift checks are
also retained. A focused report-phase regression omits the remote-only 30-day
phase in local-only mode and retains the local 7-day/128MiB threshold. All fixtures
deny network/R2 boundaries rather than hiding
credentials; no live cleanup/queue collector/source importer/R2 contact runs.
