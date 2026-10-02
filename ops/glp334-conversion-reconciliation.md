F12 reconciles supplied local conversion evidence within the existing weekly
producer. `run(..., reconciliation_input=...)` adds `conversion_reconciliation`
to `RESULT.json`; both the producer CLI and pinned job accept the optional
`--reconciliation-input /path/to/evidence.json`. Without it, the result is
`UNKNOWN`, with a missing-input gap and null event count and financial amounts.
No receiver, D1 reader, webhook, tracking, payment, Sheet writer or new collector
is added. The scheduled shell retains its current invocation. Kael owns merge,
installation, source mapping review and live execution.

The local JSON handoff uses `schema: glp-f12-reconciliation-input-v1`, a nonempty
`evidence_ref`, `complete` (an explicit boolean), `conversions`, and optional
`clicks` / `financial_records` lists. This is an evidence input contract, not a
claim that a current collector already produces these fields. All output is
labelled `SUPPLIED_LOCAL_EVIDENCE_NOT_VERIFIED_PRODUCTION_DATA`, and binds the
entire input and weekly observations by SHA256. `SUPPLIED_COMPLETE` describes
only the caller's export claim. Global coverage stays false and fleet amounts
stay null, including an empty export or one containing only excluded events.

Supported edges:

- Conversion rows use stored ledger columns `revenue_event_id`, `conversion_id`,
  `network`, `program_id`, `cs_click_id`, `network_click_id`, `event_type`,
  `event_status`, and `attributes_json`. Only `accepted_conversion` is admitted.
  An explicit `REAL` classification and typed `account_id` / `tenant_id` must
  come from reviewed source evidence; they are never inferred from absence of
  TEST flags or borrowed from `network_account_ref`. The exact four-part tuple
  must match one verified reporting registry identity, with tenant kind,
  provider slug and evidence reference. Missing or ambiguous identity remains
  UNKNOWN; Fella remains excluded from financial joins.
- A commission needs that same typed tuple, a distinct native
  `provider_event_id`, the exact `conversion_id`, and a string
  `conversion_join_evidence_ref` establishing the shared native ID meaning.
  Equality between a receiver conversion ID and a provider event ID supplies
  no edge. Existing click identifiers, if present on both rows, must agree.
  Duplicate native conversion identities, conflicting financial revisions and
  multiple commissions never select a last winner. Identical evidence repeated
  in overlapping weekly/lookback/month observations is deduplicated.
- Financial evidence requires explicit `authenticated: true`, `REAL`, `GLP`,
  `receipt_sha256` and `receipt_pointer`, consistent with the existing v2
  provenance contract. These are supplied assertions, not live verification.
  Integer minor units with a supported currency preserve explicit zero;
  missing, boolean or textual minor-unit amounts remain null.
- A commission's string `payout_id` and `payout_join_evidence_ref` must establish
  settlement transaction membership. The exact payout ID must match one
  settlement in the same typed tuple, with authenticated provenance, REAL/GLP
  classification, paid state and matching currency. A status, amount or
  account-wide total supplies no membership edge. `payout_total_minor` means
  the whole settlement, explicitly labelled
  `WHOLE_SETTLEMENT_NOT_CONVERSION_CASH`; `cash_minor` stays null because no
  per-conversion allocation contract is established.

`__receiver_selftest__`, explicit TEST and probe markers always remain excluded.
The known v2 TEST provider ID is excluded for either financial record kind.
Click TEST/probe inheritance requires exact `cs_click_id -> click_token`, network
and program equality. Conflicting rails/programs cannot transfer markers;
duplicate exact clicks remain ambiguous. Attributes that cannot be parsed
refuse the financial join. No selftest ID prefix, provider-name, timestamp,
program-only, amount-equality or paid-status join is introduced.

Verified source limits at main `42c6ff0bd6c2a37f75c04022e4c9adbfa85b2d80`:

- `systems/analytics-brain/collector.py` establishes the D1 ledger field list
  and exact click/rail/program exclusion join. It supplies no tenant identity,
  settlement membership or payout ID. The raw receiver/schema source is absent
  from this curated checkout; `/home/ubuntu/business` is unavailable here.
- `affiliate_network_access._projection` drops provider event IDs; CJ retains
  commission IDs but no conversion linkage. v2 normalization and import retain
  financial provenance but discard click/conversion linkage. PartnerStack has
  separate transactions/rewards/payouts without membership. Refersion queries
  `conversion.payment.id` but currently drops it from the projection.
- Affiliati has no verified typed registry identity or verified financial
  projection. Current report completeness cannot fill those missing edges.
- Kael's 14-event readback is context, not an input artifact or production data
  proof. Offline tests model 12 selftests and two exact Affiliati TEST rows.

The exact receiving-side input blockers are authenticated receiver-to-network
field mappings, verified account/tenant/program identities (including Affiliati),
and source settlement transaction membership / payout ID evidence. Kael must
provide these before the corresponding production edges can be supported.
Nothing in this source change resolves those missing inputs or authorizes a
live collector run, replay, mutation or installation.

The existing GLP333/GLP44 publication generation remains immutable at its prior
source. It correctly refuses the changed F12 payload. Its historical preview
tests use the actual reviewed candidate commit, preserving every original
source, producer, maintenance and tamper check; a separate regression confirms
that the current F12 checkout cannot reuse that generation. Receiving-side
publication needs Kael's separately reviewed exact F12 source procedure.
