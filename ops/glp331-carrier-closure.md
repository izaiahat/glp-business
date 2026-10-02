# GLP331 scoped carrier closure in the existing pin producers

Source-only proposal/implementation. **Not live adoption approval.** PR117's exact
seven business files are unchanged. `wrapper_pin_sync.py` now retains every pin
occurrence (the writer's scalar and associative sites both survive). The existing
`fleet_pin_release.py` retains its default full-fleet behavior and gains only the
literal `glp331` scope: seven PR117 files, one external carrier, one native job hash.
It is not a new installer, arbitrary scope selector or scheduler pause mechanism.

## Independent preparation and review

Run from a clean committed producer checkout after owner admission permits even
read-only inspection of the live target. Preparation has **no acceptance decision**:

```
python3 scripts/fleet_pin_release.py --scope glp331 --prepare \
  --repo /path/to/reviewed/clean/checkout \
  --source-head 727ad1f6af8e1000f04b96f1c267e5b0de2a832f \
  --prepared-by ACTUAL_SOURCE_AUTHOR
```

A separately designated reviewer must independently inspect producer head, all
seven source hashes, all eight emitted outputs, exact preimages/modes/absences,
full registry and target job, unchanged pinned dependencies, and caller exclusion.
The new receipt schema is `fleet-pin-glp331-review-v1`, containing the emitted
`proposal` object without alteration, a distinct `reviewer`, `reviewed_at_utc`
(UTC, at most 24h old, not future), nonempty `evidence`, and `writer_exclusion`.
`decision` is `REVIEW_ONLY` for preview or `ACCEPT_EXACT_CURRENT_BYTES` for a later
separately authorized adoption. This document emits no acceptance or approval.
Old GLP331 RESULT reports do not satisfy this native receipt contract.

The consumer validates the receipt using `--scope glp331 --repo ...
--review-receipt ...` (preview by default). `--apply` still needs explicit later
parent/operator live authority and a fresh independent exact review. No root/job
selector or `--registry-json` escape exists in the scoped CLI. The Python API's
root/scheduler arguments are for disposable fixtures only.

## Fixed closure and fences

The seven source outputs are exactly `affiliate_approval_auto_reconciler.py`,
`affiliate_program_withdrawal.py`, `affiliate_sheet_batch_writer.py`,
`affiliate_terms_fanout.py`, `affiliate_withdrawal_candidate.py`,
`glp_sheet_sync_drain.py` and the reconciliation snapshot. Scanner and metadata
are authenticated unchanged dependency inputs, not installed source outputs.
Carrier target/predecessor hashes are respectively
`45ad2a9b2c435dc96725456721f688c37cdd73d8f58e57351960c242c4d6e4a9` and
`119fdf2c23d748888cf29e6449e4e7a149826141213e944e38ea8a1e122b0e6d`.

- Exact accepted source head is PR117 merge `727ad1f...`; no new withdrawal logic.
- Fixed installed predecessor carrier `119fdf2c...`, mode `0700`; target bytes are
  read from the accepted Git snapshot, not generated from current installed drift.
- Only `f0c6a95a371c.script_sha256` may change. Its enabled/scheduled 07:15 daily
  registration, next occurrence, errors/routing/extensions and every other job,
  schedule and provider hold stay intact. Duplicate registrations are refused.
- Full-registry preimage recheck, native fire locks for the reconciliation, inbox
  and approval consumers, native registry lock, existing consumer lockfiles,
  importer directory-inode lock, native claims/in-flight and `/proc` reader census.
- Reuse maintained `maintenance_cohort.switch_files(..., commit=...)`: existing
  sources and caches are sealed before any rename. New sources remain absent until
  the cohort is sealed; private same-directory aliases are staged by that same
  cohort and linked to absent public paths as sealed inodes during commit. Their
  native save/readback occurs before any public member can reopen. On success
  private links are removed, leaving single-link final files in reviewed modes.
- Reuse native `_save_jobs_unlocked` once, compare full jobs readback to the exact
  candidate, and retain the DAC seal/custody on save uncertainty or bad readback.
  Durable intent refuses replay and forbids automatic rollback across external
  commit. Reconciliation is an owner operation, never inferred from failure.
- Every successor shell pin convention is scanned; both writer occurrences are
  required. Unknown pins, missing/drifted in-scope dependencies, orphan bytecode
  for absent sources, changed sources/modes or review preimages fail closed.

These primitives are a bounded **unprivileged** caller boundary, not isolation
against root, hostile same-UID chmod, or already-retained source in memory. The
owner must retire old/nonparticipating importers and exclude direct retained
callers. The reviewer must verify that exclusion; a string alone is not evidence.
The parent currently owns F09 cutover admission. No live importer or health launch
is part of this PR; later imports must use the parent's frozen PR113 path and its
`source_lock`, never old operational entry points or `_sync_locked`.

## Proof

`tests/test_glp331_carrier_closure.py` runs the original two-co-pin regression,
read-only preparation and exact-record refusals, and real native disposable
success/raise-before/raise-after/bad-readback cutovers. All eight public outputs
are unreadable inside the saver, native consumer claims are refused, the source
importer is blocked, non-target holds/schedule are unchanged, and duplicate
release is refused. Acceptance objects in these tests are explicitly synthetic
fixture authority only. No host acceptance receipt is produced.

The exact-head workflow checks out public native fixture commit
`07bf28d6446025d3b3b16c758ca4a300ed6db9ba`; the native job storage/fire-lock code and
its leaf dependencies are byte-identical to the installed unchanged native source.
Tests run only disposable roots and never execute business wrappers or providers.
The retained fleet regressions verify the default full-fleet path remains intact.

## F1/F2 dependency successor (2026-10-01; source only)

The seven PR117 source blobs, including its `45ad2a9b...` carrier snapshot, stay
byte-identical. That snapshot hash is **not** the newly emitted external carrier
hash: the existing scoped producer derives the latter with exactly four reviewed
hash-span substitutions declared in `GLP331_DEPENDENCY_BINDINGS`. Each legacy pin
must occur exactly once; each proposed dependency must equal its literal reviewed
SHA, the exact producer-head Git blob and that head's source-manifest entry.
Missing/drifted inputs refuse; they are never installed by this producer or
replaced with current-on-disk hashes. Other pins and both writer co-pins remain.
The proposal now carries each dependency's old/new SHA and offset for independent
byte reconstruction. Full-record CAS, seals, durable intent and no-replay remain
unchanged. Prepare only after the separately reviewed dependency source import;
this change supplies no new installer or host preparation authority.

- F1 accepts only the two exact supported v2 derive signatures (five arguments,
  or six with `commercialLabels = {}`); all existing receipt/identity/source and
  marker checks remain. Unknown signature/source still refuses before mutation.
- F2 curates the authentic installed refresher predecessor first, then binds its
  imports to the existing protected current-base graph: 17 source modules and
  three exact selection/terms/price-basis inputs. It refuses a mismatched or
  aliased root before any candidate ESM import. It does not repair, repin or
  revive the GLP34 candidate whose legacy VERIFIED overrides selected HOLD.
- The worker's actual calling path still reads the authenticated current pointer,
  resolves an authorized batch override if present, materializes detached
  incremental candidates, and passes that candidate via `--release` at its two
  existing refresh calls. The dated root in the manifest is provenance, not a
  universal current root. Only descendants carrying the exact protected
  code/selection contract pass; a future changed contract requires fresh review.
- Refresher historical bytes are commit `8f81f36` before repair. Full selection
  graph hashes and authenticated current-pointer custody are bound by the
  refresher's `GO_REFRESH_CONTRACT` literal and its source-manifest contract
  digest; no mutable installed global manifest is used as runtime authorization.
- PR37 cardinality `83cb9e73eeeeb184043ff9bf53f68265c22c51e104bb7aeb76eb9cae1e98d6aa`
  and PR35 incremental builder `a58ac5ecae59727c0106590305188e3db47af78fb399b043004a56fdf3cf8432`
  stay unchanged. Backlog worker is a dependency, not an eighth PR117 source.

The bounded source report is
`reports/ops/glp331-dependency-closure-fix-20261001/RESULT.json` in the operational
report tree. Its original native projections/actual copied-root refresher proof
are distinct from CI's portable signature/import-refusal controls. A synthetic
Longevity legacy approval conflicts with the existing exact selected HOLD and
still emits held; that fixture does not grant live authorization. Keeper-owned
maintenance/admission/inventory source, live dependencies/carrier/pins/baselines,
current release and jobs are untouched. Independent parent review and actual
runtime acceptance remain required before any coherent cutover.

## GLP93 current-base dependency successor (PR138; source only)

Only the fixed worker successor in `GLP331_DEPENDENCY_BINDINGS` advances from
`59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05` to
`83ee8b438ef7b11850a8357b0938753db414267470a00220054eb10c87bb1a9b`.
The external carrier's legacy worker span remains `1f698853...`; preparation
must still authenticate the new dependency against its literal SHA, the exact
producer-head Git blob, that head's manifest, and receiving installed bytes.
The producer installs neither worker version. There is no fallback accepting
both hashes, skipping the pin, changing the source cohort, or sweeping drift.

`GLP331_SOURCE_HEAD=727ad1f...` continues to authenticate **only the seven original
PR117 outputs**, not approval of the new worker. Those blobs, carrier preimage,
source snapshot, other three dependency bindings, scopes, generation identity,
CAS, alias/seal custody and no-replay gates remain unchanged. Historical source,
producer and manifest at immutable predecessor commit
`ae6a761c53c035e5fd7a79b3a6f2af2ba85378b7` retain the `59e25a8c...` contract and can
reproduce old proposals using their original roots/preimages. Retained reports
and receipts are not rewritten. Historical receipts do not authorize a successor:
producer-head and exact proposal comparison require a fresh separately designated
independent source/runtime review and later receiving-side owner adoption.
Changing this fixed dependency does not create a second generation or reset an
existing uncertain cutover intent.

The added regressions authenticate predecessor and successor Git/manifest custody,
retain the exact seven-source assertion, and refuse old receiving worker bytes,
a coherent-but-old Git/manifest/input graph, and a historical producer proposal
before custody, source creation or scheduler writes. Existing native success,
uncertain-save, seal/alias and replay controls are unchanged.
