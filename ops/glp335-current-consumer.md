# GLP335 existing registered consumer repair

`affiliati_postback_ship` remains the existing bridge action, with no arguments,
called by `glp_host_ingress` and drained by `glp-host-custody.service`. The registered
consumer no longer renders or overlays the superseded August receiver.

## Exact maintained outcome

Reconcile the independently accepted modern receiver in current F06 terminal
`8bb93c91-59fe-4a3b-b050-a956479c6ef5` / site head
`2359d8db82563032030d55dc35e2937ab4883357`. Require maintained network schema
`glp.postback_network_spec.v1`, authoritative `subid=#s2#`, compatibility
`click_id=#s2#`, separate `placement=#s1#`, existing query-key auth, account 15309,
allowlisted offers and GLP_DB. Exact terminal/manifest/receiver pins authenticate
this single accepted cohort; source-dependent account/offer/program and TEST
semantics are preserved unchanged, not reimplemented in this consumer.

Verify local terminal, manifest, receiver/contract bindings and live canonical
project plus exact deployment detail. Emit `SKIP_ALREADY_INSTALLED` only when all
agree; return the real publication terminal binding in the job receipt. Unknown
cohorts and mismatches refuse, with no deployment fallback. The receiver's older
candidate/remote-migration fields are historical source facts, not reclassified
by this consumer. No receiver code, store, runtime binding or carrier is changed.

## Installation and authority

The first branch commit curates the byte-exact installed predecessor:
`296f63fabee926f9adab6c15d66927d5650fae9442ce0e686d39ceddf6c32a95`.
After independent exact-head acceptance/authorized merge **retaining the original
predecessor commit (not a squash)** and actual parent receiving release, use the
supported scoped importer from a full-history merged checkout:

```
python3 scripts/sync_from_repo.py --checkout <fresh-merged-checkout> --dry-run \
  --only scripts/glp_affiliati_postback_ship.py \
  --adopt-reviewed-baseline scripts/glp_affiliati_postback_ship.py
python3 scripts/sync_from_repo.py --checkout <fresh-merged-checkout> --apply --wait 900 \
  --only scripts/glp_affiliati_postback_ship.py \
  --adopt-reviewed-baseline scripts/glp_affiliati_postback_ship.py
python3 scripts/sync_from_repo.py --checkout <fresh-merged-checkout> --dry-run \
  --only scripts/glp_affiliati_postback_ship.py
```

No importer may run while the parent's Pallas receiving hold remains. Preserve
its selected sources, whole sync-state, in-flight cleanup and protected recovery
archives. Do not bypass an unknown predecessor or change adoption baselines.

Before a genuine host intent, reconcile durable host rows and n8n legacy release
callers/waits/callbacks with complete pagination. The parent must accept the exact
intended release scope/authority and its input bindings; a task, fixture, timer,
read-only candidate probe or old publication is not a genuine registered intent.
No new deployment solely for evidence. Keep ingress's empty-argument contract,
durable ACK, uncertain-effect reservation and no-replay behavior unchanged.

After authorized genuine ACK: manually start the existing custody service,
read the exact intent and bridge job, authenticate its consumer receipt and real
F06 terminal binding, and independently re-read the canonical deployment. An
uncertain POST is reconciled by its original identity, never retried. F03's real
queue4706/Sheet308 acceptance is retained without another write. The n8n article
carrier stays inactive and conversion receiver remains untouched. Parent alone
closes. This source-only change does not itself claim those live outcomes.
