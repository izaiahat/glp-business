# GLP337 fixed shared cron-bridge custody successor

Source-only successor to `375491444eca0dd5f159eaa09f5379a055e12e3d`. This PR is **not** receiving-host acceptance or adoption authority.

## Fixed scope

- `cron_bridge_action.py`: preserve every reviewed updater admission, domain-receipt, ambiguous-effect/no-replay gate and analytics state-lock line. Only the host Sol model literal changes to `gpt-6.1-sol` (provider, xhigh effort and subscription-only transport unchanged).
- Launcher precondition: existing baseline `4e8da708…` plus authentic installed literal derivative `f00f6675…`, not an arbitrary baseline reset. Successor is exactly `f1055dea…`.
- Paired cadence precondition: authentic installed `61ca5976…`, baseline either absent or exactly that hash. Cadence successor is unchanged reviewed `4d51a627…`. An absent baseline uses existing committed-history adoption; the launcher never uses missing-baseline adoption.
- The source inventory changes only the 20 exact launcher source pins and six cadence source pins to these known curated candidate hashes. No identities, schedules, other sources or live inventory change.

## Maintained protocol

`scripts/shared_bridge_predecessor_migration.py.prepare(checkout, receiving_root)` is read-only. It binds exact checkout HEAD, committed pair and complete source manifest; destination pair, full sync state, full installed inventory, action registry and bridge bytes/modes; and exactly 20 unique existing launcher bindings. It does not identify resident bridge code from disk or claim scheduler exclusion.

`apply` requires distinct regular-file independent-review and separate owner-authority receipts with the exact proposal. The review schema is `glp337-shared-bridge-review-v1`, decision `ACCEPT_EXACT_MIGRATION`, distinct nonempty `prepared_by`/`reviewer`, plus `proposal`. Authority schema is `glp337-shared-bridge-authority-v1`, decision `AUTHORIZE_EXACT_MIGRATION`, nonempty `owner`/`writer_exclusion`, exact review-file SHA256 and `proposal`.

There is deliberately **no apply CLI**. The receiving-host installation owner must separately supply an admitted exclusion context. It must exclude all 20 launcher readers/writers, both old and new analytics caller state fences (`analytics_brain.lock` and `analytics_brain.state.lock`), direct cadence readers, and resident bridge admissions; preserve queued/pending effects without replay. A test context yielding True is fixture evidence, never live exclusion. Disk hashes or receipt text cannot establish resident identity or exclusion. The GLP44 owner must authenticate the live cadence/bridge graph and exclusion before invoking this API.

Within that separately owned exclusion, apply uses existing importer `source_lock`, strict regular-file preimage checks, content-addressed custody, metadata-preserving staged imports, state recheck and exact destination readback. Only the fixed launcher/cadence pair and their sync-state entries can change. The private importer handoff independently refuses wider selection, arbitrary target hashes, moved baselines or cadence bytes. Ordinary importer behavior still rejects f00; no `--force` or caller-supplied arbitrary-hash option exists.

The installed inventory, registry, bridge and global installed packaging-manifest snapshot are unchanged and retained as custody context. A separate reviewed inventory installation/rebuild and exact 20-row readback remain the host owner's responsibility. This avoids claiming that the scoped pair import refreshed unrelated packaging or installed newer bridge code.

If any preimage/HEAD/metadata changes, stop for exact new review. Import failure may leave a partial paired import: custody is retained, automatic rollback/retry is forbidden, and the owning installer must reconcile before admitting readers. This protocol is not an atomic multi-file transaction against noncooperating writers.

## Evidence boundaries

Offline regressions cover authentic paired import, existing-baseline rejection, exact scope refusal, review/authority and exclusion refusal, moved inputs (including full inventory/graph and modes), custody and metadata, literal routing/no-replay, all 20 source bindings, and synchronous caller/child fence compatibility. Existing updater and GLP44 reader regressions remain exact-head CI controls. No natural tick, provider call, send, publish, scheduler edit, receiving-host import or merge is authorized by these tests.
