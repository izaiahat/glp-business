# F09 guarded carrier producer (GLP-337)

This is source/CI delivery, not independent acceptance, merge permission or a live adoption receipt. The producer is `scripts/scheduled_job_health_carrier_release.py`, registered for `/home/ubuntu/.hermes/scripts/scheduled_job_health_manager.py` and **only** `b86ec8937c20.script_sha256` in the existing scheduler registry. No new job, schedule, profile, agent, sends, reruns, repins of other jobs, gateway or Rook restart.

## Maintenance seam: blocked, not a supported release mode

The operational PR112 producer refusal is reproduced by
`test_paused_record_deadlock_refuses_even_matching_independent_receipt` for both
preview and apply. An ordinary paused record, even with an unchanged schedule and
matching independent receipt, refuses before custody, output writes or the saver.
This regression preserves exclusion requirements; it does **not** implement the
requested maintenance release. Do not execute the installation steps below until
a supported admission contract exists. Source/CI review is not that contract.

Accepted PR109 dependency inspected: `c400ca1363c0074af53618ba9722b23c85e728f5`.
Its separate integration owner retains ownership of all maintenance helpers.
No changes to that branch/helpers, no merge, and no live maintenance are authorized
by this F09 source-only work. Keep the main merge hold during PR109 integration.

The existing primitives are not an F09 target-specific capability:

- `maintenance_inventory.affected_paths()` reads only the GLP44/73 declarations;
  F09's carrier and five inputs are not declared as an exact F09 scope.
  `ActiveWindow.accept_file_updates()` enforces that same allowlist.
- `maintenance_window.control_tools()` recognizes only its existing GLP44/73
  controllers, not the F09 producer or scoped importer. `require_active_window()`
  has no reviewed target/source-scope argument.
- `maintenance_cohort.switch_files()` seals only during replacement, then restores
  source modes before returning. It is not a continuous fence during refreshed
  preview, independent acceptance, apply, native save and final readback.
  `process_readers()` and `native_runs()` provide drain evidence, not first-read or
  source-writer admission. The existing F09 direct entry/import and importer do
  not participate in the GLP44 reader lock.
- `ActiveWindow.original_job()` reconstructs controls from the journal onto the
  *current* record; it does not authenticate an immutable original full-record
  preimage. Restoration preserves control-field presence, but permits other
  runtime fields to evolve. That is insufficient for F09's exact original-record
  contract even if normal schedule restoration is supported for PR109's scope.

### Minimal missing owner API (requirements, not executable commands)

The maintenance owner must extend the existing implementation, not add a daemon:

1. A reviewed **target-scoped lease** accepted by the existing active-window API,
   binding F09 job `b86ec8937c20`, the external carrier, the exact five `SOURCES`,
   source/producer heads, immutable controller/importer identities and the
   admitted-caller closure. The lease must close admission before first reads and
   drain admitted/queued executions; an idle snapshot or assertion cannot mint it.
2. An existing-cohort **hold/read/switch/release** lifecycle that excludes direct
   CLI/imported `run()` readers and overlapping scoped-import writers continuously
   through preview, reviewer handoff, apply, native saver and exact readback. It
   must provide authenticated source reads to the controller while held and must
   not reopen sources when `switch_files()` returns. The real launcher/importer
   boundaries need owner-installed enforcement before their first hash/import;
   producer-only checks cannot fence nonparticipating entrypoints or old residents.
3. An immutable **full original job capsule**, exact held-record CAS, and native
   hash-only save/restoration operation: restore the entire original record,
   including optional-field absence, scheduling cursor and runtime fields, with
   only the independently accepted `script_sha256` changed. Keep claims/drain,
   receipt/CAS/custody, native-saver uncertainty and no-replay reconciliation intact;
   do not use generic pause/resume or normalization to recreate the schedule.

Only after those APIs are reviewed/integrated may this producer explicitly depend
on them. A receipt must bind the lease and original capsule; the original record
may satisfy the enabled/scheduled identity gate only under that authenticated
lease. A plain `enabled=False` exception, reconstructed expected job, forged
ACTIVE journal, same-UID cooperation assertion or producer-local flock is not a
substitute. The F09 owner stops before editing these shared APIs and returns this
exact dependency to the parent/maintenance owner. No deployable proposal or live
acceptance is emitted by this change.

## Transport source provenance (GLP-337/338)

`ops/n8n_dt.py` is admitted alone, byte-for-byte from the existing host predecessor `/home/ubuntu/business/ops/n8n_dt.py`; SHA256 `3c029b4441de0928dca4294077c9a351b4c3b17d50969cc3b4ee84ffbce5569c`, observed permission mode `0644`. It was previously absent from curated main `692175a493c3465cf9e7798c3960924d71d7eed4`. No transport rewrite, credentials, environment files, state or broad ops-tree admission accompanies this source preservation. The budget's `health_check(get=None)` imports `_native_http` from this module before its cache/state path; it therefore requires the authentic transport even on a cached tick. That module imports only Python standard-library modules; no additional project module is required for this new branch. Existing manager transports remain unchanged and separately held.

## Contract and custody

Preview reads only; apply requires an independent receipt naming the exact producer head, reviewed source ancestor, emitted hash, installed source hashes and complete target job record. The executing producer and shared primitives are checked against that head. The source set is exactly five files: the carrier snapshot, drift checker, inventory, `ops/n8n_execution_budget.py` and its actual transport `ops/n8n_dt.py`. Both `source_sha256` and `source_mode` must name exactly this set; no omission, extra path or arbitrary hash is accepted. Every hash must match committed reviewed source bytes and installed inputs; every permission mode must match independently reviewed installed inputs. The same observed bytes/modes are rechecked under the existing native locks before custody/write and retained in custody. Dependencies are read-only producer inputs, not emitted outputs. A dirty producer checkout, unknown predecessor bytes/mode, moved source or job record, native run/fire claim, active run, wrong binding, cadence, no-agent flag, or absent registration refuses before custody/output changes.

The fixed carrier predecessor `d652c1d7bd3b6968af117d36ebbdd39ce119aa6c9b0fcadb0f0fd79191370dc9` is the snapshot in PR #103's parent. Apply preserves complete predecessor bytes/modes and staged successor bytes under `reports/ops/pin-archive/scheduled-job-health/<review-sha256>/`, performs a full-record CAS under the native scheduler's thread/flock locks, calls its native saver and reads the exact result back. Only the carrier and that target job's hash change. A pre-save error restores only owned successor bytes if the registry remains unchanged; an uncertain saver result is never replayed or blindly rolled back. Read custody and the exact target before any recovery.

The scheduler lock is **not** exclusion of direct script invocations or source-import writers. Before adoption the scheduler owner must admit a short maintenance window, exclude direct carrier/source writers and executions, and supply `writer_exclusion` evidence. Enabled every-15m cadence is retained; no disabling or lifecycle alteration is performed by this producer. A tick advancing the full job record invalidates a receipt and requires fresh independent acceptance, not bypassing CAS.

## Exact install steps (owner, after independent acceptance and authorized merge)

1. Verify independent acceptance and green Actions checkout logs against the PR's exact head. Verify the entire `ops/rebuild-source-manifest.json` against tracked committed bytes. Use a clean checkout of the accepted head, retained as `$REPO`; fetch/verify reviewed `origin/main` contains it. Do not use a working-tree candidate as live release authority.
2. Import only these paths with the registered business importer, **dry-run → apply → dry-run (`diffs=0`)**, keeping the live global packaging manifest and unrelated source/state intact:

   ```sh
   python3 "$REPO/scripts/sync_from_repo.py" --checkout "$REPO" --dry-run \
     --only scripts/scheduled_source_drift.py \
     --only scripts/scheduled_job_health_carrier_release.py \
     --only ops/scheduled-source-drift-manifest.json \
     --only ops/hermes-cron-scripts/scheduled_job_health_manager.py \
     --only ops/n8n_execution_budget.py \
     --only ops/n8n_dt.py \
     --only ops/registered-producers.json
   # Repeat identical arguments with --apply, then --dry-run.
   ```

   If an existing selected file is absent from sync state, use `--adopt-reviewed-baseline PATH` on both dry-run/apply **only** when live bytes match a prior committed revision. A conflict is a stop, never a forced copy or baseline edit. No carrier hand-copy.
3. After import, establish scheduler-owner writer/execution exclusion and read the complete current `b86ec8937c20` record. Independent reviewer creates `$REVIEW` with these fields:

   ```json
   {
     "schema": "scheduled-job-health-carrier-review-v1",
     "decision": "REVIEW_ONLY",
     "author": "implementation worker",
     "reviewer": "different independent worker",
     "reviewed_at_utc": "<UTC YYYY-MM-DDTHH:MM:SSZ>",
     "evidence": ["<exact-head CI and independent acceptance receipts>"],
     "writer_exclusion": "<scheduler-owner admission evidence>",
     "producer_head": "<accepted exact 40-hex head>",
     "source_head": "<reviewed source 40-hex head; same head or ancestor>",
     "source_sha256": {
       "scripts/scheduled_source_drift.py": "<committed SHA256>",
       "ops/scheduled-source-drift-manifest.json": "<committed SHA256>",
       "ops/hermes-cron-scripts/scheduled_job_health_manager.py": "<committed SHA256>",
       "ops/n8n_execution_budget.py": "<committed SHA256>",
       "ops/n8n_dt.py": "<committed SHA256>"
     },
     "source_mode": {
       "scripts/scheduled_source_drift.py": 493,
       "ops/scheduled-source-drift-manifest.json": 420,
       "ops/hermes-cron-scripts/scheduled_job_health_manager.py": 493,
       "ops/n8n_execution_budget.py": 420,
       "ops/n8n_dt.py": 420
     },
     "predecessor_sha256": "d652c1d7bd3b6968af117d36ebbdd39ce119aa6c9b0fcadb0f0fd79191370dc9",
     "predecessor_mode": 493,
     "expected_job": "<complete current job object, not this placeholder>"
   }
   ```

   `source_mode` and `predecessor_mode` must be actual independently reviewed decimal permission modes (`0..511`, without special bits); all sample values are illustrations, not assertions of live modes. Legacy three-source receipts refuse; create a fresh independent review, never append unchecked hashes. For the newly admitted transport, retain the provenance commit and require exact byte equality to it when adopting the reviewed baseline; no force-copy or live environment capture.
4. Preview with `python3 "$REPO/scripts/scheduled_job_health_carrier_release.py" --repo "$REPO" --review-receipt "$REVIEW"`. Reviewer verifies emitted hash, preserves the reviewed preview, sets `decision` to `ACCEPT_EXACT_CURRENT_BYTES` and adds the exact `proposed_sha256`; the implementation author must not issue this acceptance. With the exact record still unchanged, run the same registered command with `--apply` once. Preserve acceptance/custody, verify full target record delta is only `script_sha256`, installed hash equals registration, modes preserved, and every other job unchanged. Do not replay a refused/uncertain apply.
5. One registered **read-only checker** run on the installed host: `python3 /home/ubuntu/business/scripts/scheduled_source_drift.py --root /home/ubuntu/business --hermes /home/ubuntu/.hermes > /home/ubuntu/business/reports/ops/cleanup-p0-drift-check-schema-fix-20261001/registered-host-run.json`. Capture the exit code (`0=CLEAN`, `1=DRIFT`, `2=UNKNOWN`); a nonzero status is factual drift/unknown evidence, not automatically an execution crash. Save nonzero `checked`, native and host-cron counts; UNKNOWN and DRIFT are valid factual findings, never proof of CLEAN. Do **not** manually invoke the normal health carrier: it also contains existing alert/rerun branches. Natural first health tick after 08:00 PT is separate daily-delivery evidence, not fabricated by changing state or forcing a rerun.

## Schema fix

All 34 live host-cron inventory rows have `{id, surface, sources, payloads}`. Script paths are in `sources[].path`; 14 rows have one source and 20 have two. The checker now resolves that shape. Unresolvable native/crontab source paths yield UNKNOWN for the affected row only while unrelated rows continue. Missing source/payload coverage remains UNKNOWN. The sanitized real-row fixture deliberately has no top-level `path`.
