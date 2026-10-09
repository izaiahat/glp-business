# R2 editorial brief — compounded tirzepatide vs Zepbound

## Authority binding

- Operation: `r2_refresh`
- Article ID: `national-wow-medium-compounded-tirzepatide-vs-zepbound-regulatory-status-20260723`
- Topic ID: `r2-54abb90fc94b77a50f58`
- Route: `/guides/compounded-tirzepatide-vs-zepbound-regulatory-status/`
- Target query: `compounded tirzepatide vs zepbound`
- PT as-of: `2026-09-03`
- Selection: `/home/ubuntu/business/reports/ops/glp76-approved-fda-successor-20261003/native-preparation/authority/01-r2-54abb90fc94b77a50f58-selection.json`
- Selection SHA-256: `e4aea9c157aef3d1f2828ed5c96b16671aef1b4eecb366ee5901feebf95ce692`
- Selection receipt SHA-256: `5c1ee18bd1b2da13c8d50a5ab9e51970508f79902b6708fe218491ef3cce3d30`
- Operator directive ID: `glp-content-mix-v1:54d0dd011a050db3e57f51ad8c62ea5931e6ec5ea5c5f563ad4966e1e8ba25b0`
- Queue SHA-256: `54d0dd011a050db3e57f51ad8c62ea5931e6ec5ea5c5f563ad4966e1e8ba25b0`
- Strategy queue SHA-256: `5763fcc1e92fb3cfe5b4175f7c1e2db38044274d3f8c4167333296d88d259ec9`
- Strategy map SHA-256: `05d215c1bd3a8656f4bc5f5534a1ad5e13934b94a649492743fd975533f2818b`
- Current content base: `/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/current-content-base.json` SHA-256 `e090daac70dae7ca9ad5c765baf500afc1ec264c1312ec4d1c357606775bc68d`

## Search and opportunity evidence

The current Firecrawl exact-query search returned ten positions. Nine normalized destinations remain after preserving the rank-10 GoodRx tracking duplicate. Eight destinations were direct-captured with `maxAge=0`; Reddit returned an unsupported-site failure and is retained as failed-closed. Search ID: `01a06a0f-bf8a-702c-9949-1413338016be`. Search receipt: `research/S0-exact-search-normalized.json`.

The exact GSC target query is observed at 4 impressions and position 68.25 with zero clicks. A second exact-article query, `is zepbound tirzepatide`, is observed at 1 impression and position 76 with zero clicks. The selection aggregate reports 8 impressions, position 53.0, zero clicks. These GSC rows justify refreshing the existing route; they do not establish demand beyond the observed window.

The same-day paid DataForSEO refresh passed with 8 provider calls and `$0.627432` spend. The exact target query has no measured DataForSEO row in the current artifact. The nearest measured topical row is `rx pro tirzepatide` (volume 50, keyword difficulty 8, competitor position 17, opportunity `B-94e69b7ce6dc17ed`) and belongs to a separate held route. It is relevance context only and is not retargeted.

## Search intent and observed coverage

The dominant intent is informational/commercial investigation: readers want to know whether shared tirzepatide wording means the products are the same, whether the compounded version is FDA-approved, how safety and quality differ, and what cost, access, or switching claims can be trusted. The ranking set repeatedly uses comparison, cost, safety, switching, FAQ, and provider-led access sections.

The article should answer the exact query in the first block, then separate four records: ingredient identity, approved product, compounded preparation, and transaction. It must keep the evidence boundary visible: general FDA statements do not establish a named compounder, a batch, individual eligibility, a prescription decision, a dose, a switch, coverage, price, inventory, or outcome.

## Fresh source pack

Primary sources are sealed under the day directory’s `research/` folder. FDA pages own general compounding, unapproved-GLP-1, dated 503B/shortage-list, and 503B proposal statements. The current FDA-hosted label and Drugs@FDA record own Zepbound identity, presentation, prescription status, and approval-record statements. Lilly’s PI is corroborating manufacturer material. The live article capture is baseline-only.

## Article delta

1. Replace ingredient-only comparison language with the four-record model.
2. Add the current FDA label identity and presentation boundary for Zepbound.
3. Add the dated April 1, 2026 503B/shortage-list statement without turning it into an undated September guarantee.
4. Add the captured 503B proposal as proposal-only context.
5. Add an evidence checklist for claims about an unnamed compounded preparation.
6. Add explicit `Not stated` boundaries for switching, dose, price, coverage, inventory, suitability, safety, and outcomes.
7. Add one new substantive outbound link to `/guides/glp-1-cost-sensitivity/`, plus contextual links to existing guides.
8. Add one exact inbound plan from the current semaglutide/Ozempic regulatory guide.

## Prohibited or omitted claims

- No product-equivalence claim.
- No named provider, pharmacy, or compounder recommendation.
- No prescription, eligibility, dose, start/stop, or switching instruction.
- No price, savings, coverage, shortage, or inventory promise.
- No individual safety, effectiveness, or outcome conclusion.
- No affiliate CTA or `/go/` route.
