# GLP96 published-provider acquisition feeder

This is an acquisition producer, not onboarding, application approval or a
second Sheet writer. The existing Monday discovery path already calls
`affiliate_auto_apply.admit_discovery`; live admission now runs this feeder
under its existing admission lock. Provider-free dry runs and injected offline
transports stay effect-free. The application submission machine is unchanged.

Registered manual entry:

```
python3 scripts/glp_published_provider_intake.py --admit-research
```

The producer reads the live first-party sitemap and provider/review/article
sources without following outgoing or tracking links, then reads the complete
live Sheet, both existing acquisition/Sheet queues (all statuses, cursor
exhaustion required), and the existing application index. Opaque network/program
IDs remain contract identities, not provider-count increments. The source-bound
publishing policy retains article roles and identity conflicts; new detail or
review identities remain explicit and incomplete until actually reconciled.

Only two previously qualified source captures can create research-only intake:
Walgreens Virtual Healthcare and Mondae. Their existing `research_only_provider`
contract hash-checks first-party facts and derives a held Sheet row. The producer
queues that existing kind, with exact natural-key readback; it does not fabricate
an affiliate rail, clinical paid event, approval, referral URL or send authority.
Everything else matches an existing intake or retains its actual held/unresolved
source role. Approved outcomes still enter the unchanged sole Sheet consumer.
Fella stays excluded. Research holds cannot become application authority.

## Authenticated Pro advice consumed

Completed job `pro-glp96-closer-a1-20261003` is bound by JOB/MANIFEST/ledger to
GLP-96, request `closer-glp96-a1`, dispatcher session `20261003_003922_8069ea`;
RESPONSE raw UTF-8 SHA256 matches its registered source reply:
`59d305a18e4da6551f2f72b3779dc21bb2066f3906115deff16e8307a0123b37`.
No resubmission, Drive-output adoption or claims from unattested files.

Adopted: use the actual acquisition boundary rather than onboarding; count the
full published population including absent Sheet identities; retain unknown
approval and holds; keep provider identity separate from rail identity; read
all existing intake statuses; deterministic logical replay; no second writer.
The missing contracts identified by Pro were resolved from maintained discovery,
research-only queue, drain and sole-writer source plus real live inventories.

Not adopted: a separate read-only-only PR that stops at advice, and treating an
already intake-connected identity dispute as permission to reopen taxonomy.
The actual issue requires the intake read-back effect. The narrow authorized
research-only queue path supplies it without applying or sending. Eden's existing
intake remains connected and its disputed historical source identity remains
explicit, not asserted resolved or transferred across domains. No new program,
queue, scheduler, external-send route, customer-visible page or fake button.

## Evidence boundaries

Focused offline tests verify source observations, missing/held/unmapped identity
semantics, queue lifecycle preservation, real scheduled-admission integration,
research-only producer→drain→sole-writer projection, and replay. Fixture output is
not live effect. Final exact-head CI, supported scoped source import, manual
registered producer run, selected sole-writer consumption and fresh repeat-run
readback are recorded separately in the task closure receipt.
