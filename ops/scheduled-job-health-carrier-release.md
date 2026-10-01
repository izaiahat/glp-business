# F09 guarded carrier producer (GLP-337)

This is source/CI delivery, not independent acceptance, merge permission or a live adoption receipt. The producer is `scripts/scheduled_job_health_carrier_release.py`, registered for `/home/ubuntu/.hermes/scripts/scheduled_job_health_manager.py` and **only** `b86ec8937c20.script_sha256` in the existing scheduler registry. No new job, schedule, profile, agent, sends, reruns, repins of other jobs, gateway or Rook restart.

## Maintenance seam: one reproduced admission blocker

PR109 is now merged at `7cdc6e1d430d99cf5b47495c85d1bb2c337fb68f`, and
PR113 contains that exact predecessor through a history-preserving merge. The
current assignment authorizes a minimal shared-helper extension; the previous
worker prohibition is no longer the reason to stop.

**Unsupported seam: continuous admission of nonparticipating direct carrier
readers/callers and overlapping scoped-import writers.** This is one exclusion
predicate, not a request to implement three new APIs.

`tests/test_f09_native_maintenance_boundary.py` executes concrete scratch-only
counterexamples against the accepted primitives:

- With every existing maintenance flock held, a fresh unprivileged interpreter
  imports a harmless single-carrier stand-in and calls `run()`, writing its
  scratch marker during the preview/acceptance interval.
- The existing cohort switch really denies that interpreter while the carrier is
  sealed. It then restores mode before returning. A second direct interpreter
  executes the successor and writes its marker before the real native saver,
  while the target remains paused, disabled, claim-free and old-hash-pinned. This
  does not depend on a mixed multi-file cohort, a fake idle lease or root bypass.
- The actual `sync_from_repo.py --apply --only ops/n8n_execution_budget.py`
  replaces an F09 input while those same maintenance flocks remain held. Its
  hash/mode CAS is real, but it does not acquire the maintenance admission lock.

The native cases use the unchanged installed scheduler in disposable homes,
assert the selected store before mutation, suppress unrelated provider-change
notifications, and restore the *entire fixture record* except its staged hash.
They cover absent/present `next_run_at`, absent legacy skill fields, arbitrary
non-control metadata and an unrelated held job. No ACTIVE journal or independent
approval is fabricated; these are native primitive probes, **not** F09
preview/accept/apply acceptance.

### Why the previous API checklist is not the solution

- Single-carrier F09 emits only the external carrier. Its five inputs remain
  read-only. Adding them to a generic adoption scope or exempting the producer
  from a process census would not close the observed direct-entry/importer gap.
- Native exact restoration already preserves optional-field absence and the
  due cursor in these real fixtures. A third capsule framework is not justified
  by those controls. F09 still needs a full-original-record binding because
  `original_job()` reconstructs controls on current runtime data; any eventual
  adapter must authenticate that reconstruction, not infer runtime immutability.
- Moving native save inside a sealed switch would close the demonstrated
  successor-before-save interval, but not direct admission during preview,
  reviewer handoff, or overlapping importer preflight. A producer-local lock,
  plain paused exception, longer drain census or `writer_exclusion` string is
  not continuous enforcement at those existing caller boundaries.

Actual first-read boundaries are the carrier's top-level project imports
(`scheduled_source_drift`, `n8n_execution_budget`) and its callable `run()`, plus
`sync_from_repo.sync()`'s preflight/hash reads and atomic replacement. The
accepted maintenance/cohort helpers do not own admission at these boundaries.
A supported extension must supply a continuously enforced hold from before
those reads through final native readback, and drain already admitted callers;
otherwise this concrete seam remains unsupported. Keep the existing producer's
paused-record refusal and no-replay/custody protections. Do not issue a deployable
receipt or run the installation steps below under the current primitives.

The blocker owner is the parent/runtime-admission owner, with the F09 owner
retaining the sole producer. No live pause, chmod, nft, carrier install, native
scheduler mutation, restart or main merge is authorized by this evidence.

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
