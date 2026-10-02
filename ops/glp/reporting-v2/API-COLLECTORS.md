The weekly job's existing roster already includes PartnerStack, Admitad,
ClickBank, Refersion and HealthTrader. `weekly_collectors` now dispatches them
through the same `AccessResult`, `ReadTransport`, credential helpers, request
budget, deadline and private page receipts as the six existing API readers.
The CLI requires all new collector modules in the reporting source pins.
The scheduler wrapper carries the corresponding successor manifest digest.

The separate [Sovrn weekly candidate](SOVRN.md) adds daily commission-day
observations and explicit no-read month/lookback results. Its seven-call budget
cannot fit the unchanged 240-second scheduler; scheduled Sovrn remains UNKNOWN
before I/O. It is excluded from this document's one-request smoke cohort.

The runner supplies the last closed UTC week, last closed month and 90-day
lookback as half-open intervals. Each reader preserves its source time axis:

- PartnerStack reads rewards, transactions and payouts separately. Documented
  epoch-millisecond creation filters use an inclusive maximum one millisecond
  before the requested end. Each resource pages using `starting_after` until
  `has_more:false`; count, identity and date inconsistencies remain PARTIAL.
  First-page empty without independent count proof remains PARTIAL. Transaction
  amounts are not commissions; payout amounts are not verified settlement.
- Admitad uses a cached publisher Bearer when present. If absent, existing stored
  client_id/client_secret can perform one fixed-origin client-credentials token
  exchange for only the read-only `statistics` scope. The token stays in memory;
  no credential cache is written and failures are not retried. The authentication
  request is separately bounded and counted, never included in page receipts.
  Missing clients remain UNKNOWN. Date-only inclusive filters and offset/limit
  pagination (500 cap) still do not establish UTC boundaries or complete revenue.
- ClickBank Quickstats reads daily summaries with explicit inclusive calendar
  dates. HTTP 206 advances the `Page` header; HTTP 200 terminates pagination.
  Response account nicknames distinguish the daily populations. Vendor timezone,
  currency and affiliate-commission semantics remain unverified, so these are
  PARTIAL summaries with null period counts, commission and cash totals.
  A literal JSON null, as returned by the bounded 2026-10-02 read, has explicit
  UNKNOWN `HTTP200_NULL_BODY_NO_POPULATION_PROOF`; it is not an empty population.
  Unknown envelopes still fail closed rather than being converted to zero.
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
- HealthTrader's official Apiary reference is now reachable through HTTPS at
  `htaffiliate.docs.apiary.io`. Its conversion search documents
  `/affiliates/v1/conversions/search`, date_from/date_to, page/per_page, conversions
  and info.total_records. The reader uses HTTPS and only the stored bearer;
  it never mints one. Count reconciliation never establishes vendor timezone,
  date-boundary inclusivity, TEST/REAL or cash; all observations remain PARTIAL.
  Currency-prefixed amounts and unknown schemas are not coerced into money/zero.
  Product/customer identifiers, invoice numbers and tracking attribution are dropped.

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
- [Admitad publisher reports](https://developers.mitgo.com/hc/en-us/articles/34481279018130-Publisher-reports),
  [client authorization](https://developers.mitgo.com/hc/en-us/articles/34481345606546-Client-authorization),
  [statistics scope](https://developers.mitgo.com/hc/en-us/articles/34481337268626-Help-on-access-settings)
  and [pagination](https://developers.mitgo.com/hc/en-us/articles/34481350125970-Limit-offset-parameters).
- [ClickBank Quickstats](https://support.clickbank.com/en/articles/10535409-quickstats-api)
  and [API specifications](https://support.clickbank.com/en/articles/10535397-clickbank-api-specifications).
- [Refersion affiliate examples](https://www.refersion.dev/docs/graphql/for-marketplace-affiliates/example-queries/),
  [queries](https://www.refersion.dev/docs/graphql/reference/queries/),
  [pagination](https://www.refersion.dev/docs/graphql/pagination/).
- [Everflow partner conversions report](https://developers.everflow.io/api-reference/post-affiliatesreportingconversions).
- [HealthTrader official conversion search and OAuth reference](https://htaffiliate.docs.apiary.io/reference/conversions/conversions-search/search-all-conversions)
  confirmed through a fresh unauthenticated browser read; the old HTTP alias was
  not sufficient evidence of an unavailable contract.

`python3 scripts/glp_api_collectors_smoke.py --output /path/to/SMOKE.json`
reserves a fresh private report and permits at most one request per network for
the last closed week. HTTP status and row count refer to one response, never
complete period totals. Admitad permits one additional fixed-origin read-only-scope
OAuth exchange if no cached bearer exists, with a separate auth count/status.
Missing credentials produce explicit UNKNOWN skips. The same output path cannot
be replayed. This is not an authorization to install the reporting generation.
