# GLP-334 projected pin closure (source only)

## Authenticated blocker and repair

PR551 (`153b1c925e8680b558e9e820b4726175f1c28467`, accepted source review
5542283910) did not change `scripts/glp_affiliate_weekly_reporting.py`. Its J7
manifest requires `060bc4e747bcaa2cc4047c8a1e77cfec184814de19561e732679c8c5051a5352`,
but the actual installed module is
`547f31d3543f72833ea470eee909b36ebb56a32283f5e3011c72a24d13256b7c`.
The latter is byte-identical to commit `ebe12ab05d72530905bd5ff96cda49d8159c5d73`
(merged PR214); it is not unexplained drift. The installed conversion consumer
also pins those historical bytes.

Maintained `060bc4e` is the exact PR523 source at
`a5149c0c3023ff459ed29212ae64373d4bb51b7a` (accepted PR head
`ff65a3c6969aac176a696e0d4c71e4fb601478c1`, merge `e8914154dd4768012e2dfb3293b30a9709a07d3d`).
Its only module delta is the nine-line guard refusing unapproved earnings unless
there is a bounded signed negative reversal. **Do not roll this back or repin J7
to the historical module.** The maintained consumer differs from installed only
in this dependency pin; its shell differs only in the resulting consumer hash.

`ops/glp/reporting-v2/glp334-install-cohort.json` declares the smallest runtime
cohort for this installed state: **14 paths**. It joins PR551's five runtime
paths to the reconciler/consumer co-pin and all four reporting manifest/shell
pairs. Base/J5/J6 manifests also pin the shared module and would otherwise
break on adoption. There is no unrelated collector behavior change. These
runtime source files already exist in reviewed main: this follow-up declares and
checks their coherent selection rather than manufacturing another source revision.
No legacy wrapper or immutable historical oracle is changed by this PR.

Read-only preflight (not an installer):

```sh
python3 scripts/glp334_pin_closure.py --checkout "$REVIEWED_CHECKOUT" \
  --installed-root /home/ubuntu/business
```

The checker overlays only declared source files **in memory**, checks all four
projected manifests against selected or unselected installed bytes, the consumer's
literal dependency pins, shell prechecks and manifest argv pins. It never rewrites
pins, emits install commands, reads credentials, invokes subprocesses or runs jobs.
`--selection PATH` accepts a JSON path list to reproduce the old PR551-only
selection. READY_SOURCE_ONLY is compatibility, not receiving/business acceptance.
Non-source retained-intake pins are still checked, never waived.

## Offline proof and limits

Portable CI fixtures explicitly substitute synthetic retained report preimages;
they do not claim actual installed wrapper equality. Separate host-local evidence
is in `lanes/F12-CONVERSIONS/pin-closure-20261016/`: authenticated Git/installed
provenance, PR551-only failure, zero-drift 14-path projection, and exact-byte shell
rehearsal. That rehearsal runs unmodified J7 and consumer shells in a read-only,
network-unshared namespace with credentials hidden and audit-denied. It stops
immediately after the real job's full-manifest verification/reporter import, or
the consumer's real pinned loader (checking null money). It does **not** invoke
a native job, generate reports, construct live collectors, advance a cursor or
prove live continuation. Full continuation/no-retry/shared-budget/earnings guards
remain covered by their unchanged focused regressions.

## Producer is not consumer; future sequence requires separate grants

- **Producer**: native `aadf6677c7f0`, `GLP K3 weekly network reporting`,
  `glp_affiliate_weekly_reporting_j7.sh` -> `glp_affiliate_weekly_reporting_j7_job.py`
  -> J7 reporter -> J7 access facade -> resumable CJ collector. Cron Monday 09:00
  America/Los_Angeles; next observed slot 2026-10-19 09:00 PDT. The shell shares
  **32 outbound attempts / 240 seconds** across all networks/authentication;
  native script timeout is **300 seconds**, not a raised HTTP budget. Reporting
  is read-only, full roster, lookback 90 days. September closed_month continuation
  precedes moving windows; completed replay is zero-request, error partitions
  remain nonretrying, and money/account financial scope remain UNKNOWN/null.
- **Consumer**: native `2ca359464bfa`, `glp334_live_reconciliation.sh`, daily
  09:15 PDT, native timeout 180 seconds. It reads current receiver evidence and
  the latest retained weekly producer receipt; **it does not call CJ**. Completed
  execution `a8e0f1ba628a43b3b68cfe62ce7ce908` must not be replayed.

After independent Astra review and exact-head CI: a separately authorized owner
may perform coherent scoped importer dry/apply/diff0 and guarded external wrapper
plus native digest enrollment, with fresh provenance/custody and no changes to the
global installed source manifest or other owners. Then, only with a separate
producer-read grant, perform one registered producer read and inspect actual
weekly output/cursor state. Only afterward, with consumer-read authority, consume
that newer retained receipt. No inferred rerun from this source grant, no claim
that unread 157 will decrease, no synthetic fixtures counted as real coverage,
no credentials/money/provider writes, and no Linear closure.
