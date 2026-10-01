# Eden factual-source closure (GLP-80)

This source-only successor replaces Eden's obsolete two-page literals with seven
assertions owned by four independently accepted official URLs. Four new
read-only captures were obtained through the unchanged registered `capture()`
transport. The capture fixtures retain exact encoded receipts, actual
`received_at`, redirect metadata, body hashes, source ownership and full
prescription/nonapproval/conflict context. No producer main or live writer was
run to obtain them; no intake or tracking link was followed.

## Interpretation and remaining representation gap

Only provider-reported conditional advertisements and provider-level injection
context are accepted. No clinical identity, indication, efficacy, pharmacy
quality, stock, actual supply, all-state availability or guaranteed prescription
is accepted. Price text is offer context, not economic renewal.

Required public note (preserved verbatim in the existing policy `finding`):

> Reported information: Eden advertises a conditional Zepbound-labelled offering.
> Its explanatory paragraph names Mounjaro; the medication actually prescribed
> or supplied has not been verified.

`finding` is retained by `verify_sources()` in the producer manifest, but it is
not a consumed public qualification. The current
`src/therapeutic-taxonomy.mjs:publicTaxonomy()` projection exports tags,
treatment state, date and digest only. Neither the row's digest basis nor this
projection has a claim-qualification field. Therefore the policy remains
`review_required`; a successful raw assertion replay is **not** permission to
refresh Eden or to publish an unqualified positive brand claim. No new
qualification framework or public field has been invented.

Separately, current Eden `source_urls` lacks the three exact treatment URLs.
The unchanged source-membership guard still rejects this. Only test-local
future-closure membership is used in the positive replay; the live registry
remains untouched, including historical source attachments and dates.

## Verification scope

- Portable authentic full-page replay: all seven assertions pass unchanged
  `verify_sources()` / `support()`; captures never concatenate pages.
- Per-brand semantic negatives remove the complete hero and bottom offer while
  retaining educational/navigation names, prescription disclaimers and the
  Zepbound-route Mounjaro conflict. Only the removed brand fails.
- Compound-offer removal rejects semaglutide, tirzepatide and compounded while
  preserving generic names/nonapproval and injection context.
- Injection-context removal rejects injection without inferring it from
  molecule options; remaining molecule/compound assertions still pass.
- Host-only native test calls the actual unchanged producer `validate()` and
  Node reconciler on a labelled single-row in-memory fixture. It also rejects
  stale evidence under the existing 31-day rule. No validator pass is stubbed.
  Hosted CI lacks private native source and explicitly skips that one test;
  its exact-head job runs the portable replay and the existing focused suites.

CareBare, Novi and Curex installed source corrections are unchanged. Source
closure alone cannot clear today's complete-registry stale denominator. This
PR does not adopt sources, renew any dates, modify commercial holds, install,
merge, build or release. Coordinator authority is required to resolve the
missing public qualification representation and future source membership;
future observations and whole-registry validation remain separate work.
