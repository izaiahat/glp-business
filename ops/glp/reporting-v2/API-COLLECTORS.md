The weekly job's existing roster already includes PartnerStack, Admitad,
ClickBank, Refersion and HealthTrader. `weekly_collectors` now dispatches them
through the same `AccessResult`, `ReadTransport`, credential helpers, request
budget, deadline and private page receipts as the six existing API readers.
The CLI requires all new collector modules in the reporting source pins.
The scheduler wrapper carries the corresponding successor manifest digest.

The runner supplies the last closed UTC week, last closed month and 90-day
lookback as half-open intervals. Each reader preserves its source time axis:

- PartnerStack reads rewards, transactions and payouts separately. Documented
  epoch-millisecond creation filters use an inclusive maximum one millisecond
  before the requested end. Each resource pages using `starting_after` until
  `has_more:false`; count, identity and date inconsistencies remain PARTIAL.
  First-page empty without independent count proof remains PARTIAL. Transaction
  amounts are not commissions; payout amounts are not verified settlement.
- Admitad uses cached publisher Bearer credentials, date-only inclusive filters
  and offset/limit pagination capped at 500. Absent cached token is UNKNOWN;
  no client credential exchange or secret refresh occurs. Even reconciled rows
  remain PARTIAL because the vendor contract does not establish UTC boundaries.
- ClickBank Quickstats reads daily summaries with explicit inclusive calendar
  dates. HTTP 206 advances the `Page` header; HTTP 200 terminates pagination.
  Response account nicknames distinguish the daily populations. Vendor timezone,
  currency and affiliate-commission semantics remain unverified, so these are
  PARTIAL summaries with null period counts, commission and cash totals.
- Refersion uses affiliate GraphQL conversions and payments with independent
  offsets and explicit terminal pages. Its exclusive `created_from` and inclusive
  `created_to` are translated to the requested UTC half-open seconds. Archived
  conversions and cancelled payments are omitted by the provider. An initially
  empty population has no independent total proof and remains PARTIAL.
- Everflow uses one shared partner reader for four isolated tenant partitions:
  `everflow_orderly` (OrderlyMeds), `everflow_ro` (Ro), `everflow_eden` (Eden)
  and `everflow_reverse_health` (Reverse Health). Each reads only its own
  `<tenant>_affiliate_api_key` partner key via `X-Eflow-API-Key`; network,
  advertiser and marketplace keys are not interchangeable. A missing key gives
  that tenant UNKNOWN (`UNKNOWN_CREDENTIAL_MISSING`) without a request. The
  conversions report pages with `page`/`page_size=50` and is COMPLETE only when
  every page is fetched and summed rows equal `paging.total_count`; a mismatch,
  changing total, duplicate or early empty page is PARTIAL. Whole-UTC-day
  windows map to inclusive `from`/`to` dates within the prior 365 days.
  Base conversions and events stay distinct; zero-payout actions are not paid
  customers; offer terms never become commission or cash. TEST/REAL and
  currency stay UNKNOWN unless a row states them. The requested `timezone_id`
  mapping was not verified, so `reporting_timezone` is
  `UNKNOWN_TIMEZONE_ID_UNVERIFIED`.
- HealthTrader remains UNKNOWN without credential or API reads: its referenced
  Apiary documentation was inaccessible and no official reporting endpoint was
  verified. Offer inventory does not establish reporting capability.

All readers retain UNKNOWN TEST/REAL and attribution when the vendor fields do
not prove those classifications. Reported amounts preserve their currency or
leave it unknown; none are aggregated into fleet commission, cash or payments.
Fella exclusions and provider holds remain in force. HTTP/provider/credential
failures are UNKNOWN, with safe reasons and no automatic retries. Receipts
contain fixed endpoint/status/hash metadata, never headers or response bodies.

Official contracts reviewed on 2026-10-02:

- [PartnerStack rewards](https://docs.partnerstack.com/reference/get_v2-rewards),
  [transactions](https://docs.partnerstack.com/reference/get_v2-transactions),
  [payouts](https://docs.partnerstack.com/reference/get_v2-payouts).
- [Admitad publisher reports](https://developers.mitgo.com/hc/en-us/articles/34481279018130-Publisher-reports)
  and [pagination](https://developers.mitgo.com/hc/en-us/articles/34481238241554-Limit-offset-parameters).
- [ClickBank Quickstats](https://support.clickbank.com/en/articles/10535409-quickstats-api)
  and [API specifications](https://support.clickbank.com/en/articles/10535397-clickbank-api-specifications).
- [Refersion affiliate examples](https://www.refersion.dev/docs/graphql/for-marketplace-affiliates/example-queries/),
  [queries](https://www.refersion.dev/docs/graphql/reference/queries/),
  [pagination](https://www.refersion.dev/docs/graphql/pagination/).
- [Everflow partner conversions report](https://developers.everflow.io/api-reference/post-affiliatesreportingconversions).
- HealthTrader's referenced [Apiary host](http://docs.htaffiliate.apiary.io)
  was inaccessible; its [official tour](https://healthtrader.com/us/tour)
  describes portal exports without an API reporting contract.

`python3 scripts/glp_api_collectors_smoke.py --output /path/to/SMOKE.json`
reserves a fresh private report and permits at most one request per network for
the last closed week. HTTP status and row count refer to one response, never
complete period totals. Missing bearer credentials and the HealthTrader source
gap produce explicit UNKNOWN skips. The same output path cannot be replayed.
