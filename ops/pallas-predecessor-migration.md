# GLP347 fixed predecessor/co-pin import

This migration handles only the receiving-host state recorded by GLP347: stale existing baselines for the sole Sheet writer, drain and reconciliation wrapper. It does not accept arbitrary existing-baseline adoption. The exact installed predecessors are retained committed source, not historical writers restored to production. The fixed five-file successor cohort is the Astra-accepted PR262 task payload at `323b92c59276fdd06a082008d61e8716d97d9a9a`.

## Contract

`scripts/pallas_predecessor_migration.py` exposes `prepare(repo, root)` and `migrate(repo, root, apply, review_path, authority_path, reader_exclusion=...)`.

- `prepare` is read-only. The proposal binds exact installer HEAD, fixed accepted source commit, every selected destination preimage/mode, importer state, installed manifest, exact-head source manifest and the running maintained producers. The helper must be absent and the registry baseline must equal its exact retained predecessor.
- An independent review receipt uses schema `glp347-pallas-migration-review-v1`, decision `ACCEPT_EXACT_MIGRATION`, nonempty distinct `prepared_by`/`reviewer`, and the exact `proposal`.
- A separate receiving-host authority receipt uses schema `glp347-pallas-migration-authority-v1`, decision `AUTHORIZE_EXACT_MIGRATION`, nonempty `owner` and `reader_exclusion`, the same exact `proposal`, and `review_sha256` binding the independent receipt. Never manufacture reviewer acceptance.
- Preview: `migrate(..., False, review_path, authority_path)`. Source and existing sole-writer exclusion are held; no state, source, custody, cron, Sheet or site writes occur.
- Apply: the receiving-host owner supplies an externally established `reader_exclusion` context manager. It must exclude consumers of the fixed source cohort throughout apply/readback and yield literal `True`. The script cannot create reader-exclusion authority. A string in a receipt or the Sheet lock alone is insufficient. The callback-less apply always refuses.
- Under external reader exclusion plus the maintained source-import lock and actual existing Sheet lock, the migration rechecks all inputs, preserves content-addressed preimage/successor custody and calls the maintained importer. An opaque process-local lease permits only the three fixed `(baseline, predecessor, successor)` exceptions. There is no public force/hash/baseline flag.
- The importer writes source and state normally. Existing file/state modes, unselected baselines, the global installed manifest and unrelated files stay unchanged. The wrapper successor co-pins both writer references to the fixed accepted writer hash. The migration does not deploy the wrapper into Hermes, change cron registrations, edit Sheet cells or release the site.
- After apply, ordinary `sync_from_repo.py --dry-run --checkout <merged-exact-head> --only ...` for the same five selected paths must return `diffs=0`. Do not replay the migration against its now-consumed predecessor proposal.

## Receiving sequence

1. Merge PR262 with green checks on its exact head and byte-identical accepted task payload.
2. Independently review this migration PR, merge only its green accepted exact head, and prepare a fresh receiving-host proposal. Review/authority must bind that exact install head; a manifest-only head change requires refreshing the exact proposal receipt, not weakening its equality check.
3. Establish consumer exclusion through the maintained host/scheduler path, preview then apply the migration, and obtain ordinary scoped zero-diff readback. A partial import/readback failure is a reconciliation stop, never automatic retry.
4. Deploy the updated maintained reconciliation wrapper and its outer registration co-pin through the existing authorized wrapper/scheduler release path. This API grants no extra activation authority.
5. Run the already-reviewed `pallas_relationship_ending.py` registered entry with the retained private queue, verify real sole-writer Sheet readback and zero-effect replay, then run actual registry sync. Build/release through the then-current canonical site carrier. Only zero Pallas commercial anchors on both live review/provider pages permits GLP347 closure.

The fixed selection is writer, drain, maintained reconciliation wrapper, registry sync and Pallas relationship-ending helper. Other curated source installs use the ordinary importer; no unrelated cohorts are swept into this migration.
