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
