# GLP-334 / F12 — CJ continuation and retained coverage diagnosis

## Scope and acceptance boundary

Money-path source change: independent Astra review and guarded install are required. Do not merge or run the candidate on the host before review. No network calls, credential creation/reset, Sheet writes, callback replays or Fella traffic are authorized by this change. Offline fixtures prove paging only, not live earnings or complete coverage.

The retained consumer receipt is `reports/glp-clinic-scout/affiliate-machine/conversion-reconciliation/20261015T161532Z-b6616693a032/RESULT.json`. It points to weekly producer `20261015T021905Z-fba58e2e0d21/RESULT.json`, SHA256 `b94d3f09c291a6f51b2896d0255312213a073b6753a21cea254a44a56c074c8f`. The complete, mechanically counted per-network/per-period census is at `/home/ubuntu/business/lanes/F12-CONVERSIONS/UNREAD-NETWORK-PERIODS.json`; its reproduction script uses only local receipts and credential-path stat metadata.

## Retained unread population

Counted **44 distinct budget-unread networks / 132 periods**, not 44 credential failures. For every network below the exact retained reason is `REQUEST_BUDGET_UNREAD`, and all three half-open UTC intervals are unread:

- closed_week: `[2026-10-05T00:00:00+00:00, 2026-10-12T00:00:00+00:00)`
- lookback: `[2026-07-14T00:00:00+00:00, 2026-10-12T00:00:00+00:00)`
- closed_month: `[2026-09-01T00:00:00+00:00, 2026-10-01T00:00:00+00:00)`

clickbank, clickdealer, daisycon, digistore24, digital-media-solutions, direct, dr-cash, ebay-partner-network, everflow, flexoffers, giddyup, goaffpro, gurumedia, health-nutrition-affiliates, healthtrader, howl, impact, katalys, levanta, linkconnector, linktrust, markethealth, maxbounty, maxweb, moreniche, partnerize, partnerstack, perform-cb, rakuten, refersion, sellhealth, shareasale, skimlinks, sovrn, takeads, target-partners, terraleads, trackdesk, tradedoubler, uppromote, walmart-affiliate-program, webgains, xentraffic, yepads.

CJ's closed_month interval above is additionally partial: exact gap `REQUEST_BUDGET_EXHAUSTED_PARTITION`; observation reason `COUNT_PAGINATION_OR_PERIOD_PROOF_INCOMPLETE`.

Eight **reached but adapter-unbound** networks have all three intervals unknown: advidi, affise-reach, amazon-associates, archer-affiliates, avantlink, bixgrow, brandchamp, buygoods. Exact reason: `NO_AUTHORIZED_API_REPORT_BOUND_NO_BROWSER_AUTOMATION`. They need an approved financial adapter/account binding, not an invented environment variable.

Thus there are **157 distinct unread/unsupported/partial network-period pairs** (132 + 24 + 1). The run-wide outbound counter is 32/32. All 29 retained HTTP status receipts are 200; three attempted requests lack per-response status receipts here, so their status is UNKNOWN, not assumed successful. Later networks were never queried, so no provider/API-error diagnosis is inferred for them.

## Budget defect and bounded repair

The registered shell passes `--max-requests 32 --budget-seconds 240`; `RunReadTransport` enforces a **per-run, shared outbound-attempt budget**, including authentication and failed attempts. It is not permission to spend 32 per network, nor an account-wide lifetime cap. Keep both bounds unchanged.

The maintained dispatch formerly reached `affiliate_network_access._other_earnings`, importing the frozen `glp_affiliate_weekly_reporting.collect_cj`. Its seven-day intervals and recursively bisected incomplete partitions were an in-memory queue, restarted on each invocation. Merely editing the duplicated J7 collector would not fix the registered dispatch. The J7 dispatch now explicitly uses the maintained resumable J7 collector; the frozen predecessor is untouched.

The continuation persists after each read: pending half-open intervals, accepted row counts and sanitized receipt projections, rejected-partition state and incomplete-response hashes. It is account/window/source bound, checksum checked, private 0600, locked, atomically replaced/fsynced, bounded to 32 MiB and expires after eight days. Rejected/API-error partitions fail closed and do not auto-retry on replay; an operator-approved new read epoch is needed after diagnosis. No token/header/raw payload is persisted. September is visited before moving week/lookback windows; completed September replay uses no HTTP request. Other networks can still be starved by the unchanged fleet ordering/budget: this patch does not claim a fleet-wide scheduler or backfill solution.

Synthetic regression: 3 complete interval-pages; run 1 consumes 2 requests and saves the remaining interval, run 2 consumes 1, completed replay consumes 0 and leaves the checkpoint unchanged. Further tests cover registered dispatch, HTTP 401/403/429/500, checksum/account/staleness/permissions/symlink rejection, incomplete-response splitting and elapsed deadline. Money and account financial scope stay UNKNOWN on successful paging.

## NEEDS_OPERATOR (names only)

Local file-existence inspection found the following maintained partner-API bindings absent (it did not read or validate credential values):

- NEEDS_OPERATOR adapter=`affiliate_reporting_everflow` tenant=`everflow_eden` vault_key=`glpclinicscout/everflow_eden_affiliate_api_key`
- NEEDS_OPERATOR adapter=`affiliate_reporting_everflow` tenant=`everflow_orderly` vault_key=`glpclinicscout/everflow_orderly_affiliate_api_key`
- NEEDS_OPERATOR adapter=`affiliate_reporting_everflow` tenant=`everflow_reverse_health` vault_key=`glpclinicscout/everflow_reverse_health_affiliate_api_key`
- NEEDS_OPERATOR adapter=`affiliate_reporting_everflow_roster_j7` tenant=`liferxmd.everflowclient.io`: **no key name is defined**; approve and implement a source-owned tenant binding first.
- NEEDS_OPERATOR adapter=`affiliate_reporting_everflow_roster_j7` tenant=`wisp.everflowclient.io`: **no key name is defined**; approve and implement a source-owned tenant binding first.

These are under the existing `~/.hermes/secrets/` namespace, not new browser-vault handles. A missing key is not permission to mint it. Eden has an alternative retained portal intake, but a dated intake is not fresh whole-population proof. Other adapter credential files inspected are present; presence does not prove valid credentials, scope, account ownership or successful unread APIs.

## Why coverage_complete cannot honestly become true

`glp334_live_reconciliation.py` unconditionally emits `coverage_complete: False` (line 413 at the reviewed base). Its receiver capture explicitly declares a non-atomic, incomplete financial population. This conservative guard is intentional and is not flipped by this PR. Both report stages require more than an API-page count.

The retained 17 receiver events are excluded as 13 SELFTEST + 4 TEST; there are no eligible REAL reconciliation rows. The registry contains **two reporting-identity-only mappings**, not an empty registry and not a fleet-complete financial crosswalk (800 unresolved typed-identity gaps). Genuine conversion/click/account/tenant/program lineage, TEST/REAL classification, invoice membership, reversals and all-rail closed settlement remain unproved. Retained Everflow native statements are explicitly not a complete reconciled financial population. Commission and cash remain null; no zero or closed period is inferred.

Jobs `aadf6677c7f0` (producer) and `2ca359464bfa` (consumer) were not run: the candidate changes money-path source and requires independent review first. Historical unread count is 44; live after/delta are UNKNOWN until reviewed adoption and a single authorized wrapper run. Preserve existing shared locks and single-writer queues. Owner: GLP-334, with GLP-251/GLP-88 for unresolved lineage and identity evidence.
