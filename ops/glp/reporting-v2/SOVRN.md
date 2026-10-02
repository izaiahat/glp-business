# Sovrn weekly reporting candidate

Ticket: `rook-v2-sovrn-reporting-20261002` / GLP-333, reviewed 2026-10-02.
This source candidate requires Kael's review, merge and installation. No live
authenticated provider request was used to implement or test it.

## Reviewed contract and unresolved response

[Transactions](https://developer.sovrn.com/reference/get_reports-transactions)
and its [Markdown/OpenAPI](https://developer.sovrn.com/reference/get_reports-transactions.md)
agree on GET `https://viglink.io/v1/reports/transactions`, one calendar day per
request, `YYYY-MM-DD` dates and at least 60 seconds between requests.
[Authorization](https://developer.sovrn.com/docs/authorization) requires a
per-site key in `Authorization: secret <key>`. The existing secret-file helper
reads the proposed slot `affiliate-marketplaces/sovrn/secret_key`. No key is
created, discovered, refreshed or logged. Installation must verify that this
slot belongs to the intended site; it does not establish all-account coverage.

The example nests `commission.publisherNetRevenue`, `commission.revenueId`
and `commission.commissionId`. The embedded OpenAPI describes a flat
`publisherRevenue` and numeric `revenueId`. Their equivalence is unproved.
This candidate accepts only the reviewed nested example as observation data.
Flat, mixed or unknown shapes fail closed without copying their payload.
No alias from `publisherRevenue` to net commission is introduced.

Only `commissionDate` is sent. The seven dates describe a commission-day
cohort for the runner's last closed UTC week; provider day timezone is UNKNOWN.
Adding `clickDate` or `updateDate` would intersect filters, not broaden them.
`updateDate` would describe a changed-record cohort rather than commission
creation. Returns can revise an existing transaction or appear as a separate
adjustment; stable identities and revision ordering are UNKNOWN. Source IDs
are retained as observations, never treated as stable ledger event keys or
deduplicated. Negative amounts remain negative, missing/nonfinite/invalid
amounts remain null. Currency, amount unit and cash/settlement semantics remain
UNKNOWN. No click, customer, product, page, tracking URL or free-text field is
persisted. Fella rows are excluded using the reviewed campaign/merchant names.

There is no proved pagination, cap, truncation or independent count contract.
Seven successful daily calls therefore remain PARTIAL, including seven empty
responses. Record count, commission and cash totals remain null;
`report_complete_period` remains false. Unknown root metadata is rejected
rather than interpreted as pagination. Wider month/90-day periods are explicit
UNKNOWN / WEEKLY_ONLY_RATE_LIMITED, with no credential or provider reads.

## Execution budget conflict

The collector declares a local 600-second ceiling, clips it to the supplied
parent deadline and requires seven request slots before any read. It reserves
576 seconds: six 61-second waits plus seven 30-second request allowances.
Each delay starts after the preceding response finishes, so request starts
are at least 61 seconds apart. The injected monotonic clock is rechecked after
sleep and before requests; no retry or catch-up request is made.

All invocations share a network-wide kernel `flock` and durable private state
at `reports/glp-clinic-scout/affiliate-machine/weekly-reporting/.sovrn-reporting-rate-limit.json`.
Using one gate for all site keys is conservative and does not require an
unproved site ID or store any key/hash. The lock remains held through every
wait and request. The same locked inode is updated and fsynced; it is never
replaced or deleted. A reservation is persisted before I/O. A known response
persists a 61-second cooldown after completion. An uncertain request leaves
`in_flight:true`, preventing automatic replay even after the lock releases.
Busy, corrupt, insecure, unavailable, boot-changed or in-flight state reports
UNKNOWN / CONCURRENCY_PROTECTION_UNAVAILABLE. Linux boot identity makes shared
monotonic timestamps comparable; unsupported hosts refuse protection. An
immediate repeat requires 637 seconds including its cooldown and fails before
I/O with RATE_LIMIT_COOLDOWN_EXCEEDS_BUDGET under the local 600-second ceiling.

The collector also installs its own clipped wall timer for an admissible
invocation. Non-main-thread execution, blocked SIGALRM, unavailable timer APIs
or any existing timer/interval produce UNKNOWN / WALL_TIME_PROTECTION_UNAVAILABLE.
It never hijacks the shared job's alarm; its own timer is cancelled and the
prior handler restored on exit. A timeout without a response receipt preserves
the durable in-flight marker. Timer and limiter state cannot authorize retries
or automatic crash-state recovery; Kael owns review of uncertain state.

The maintained runner and scheduler wrapper still use a global 240-second
budget and the CLI sets SIGALRM at budget + 5 seconds. This cannot fit even
the 366 seconds of waits. After the wrapper admits its approved source and
registry inputs, the weekly collector therefore emits UNKNOWN /
RUNNER_BUDGET_INSUFFICIENT before reading a credential or making a request.
Budget refusal takes precedence over credential diagnosis; an admissible
collector invocation with an absent key reports UNKNOWN / CREDENTIAL_MISSING.
The existing global bounds and process alarm are unchanged. Month/lookback
UNKNOWN observations are still persisted after a weekly failure.

The current-main CJ identity-registry change is separately source-only: its
wrapper retains the installed predecessor registry pin. Executing the wrapper
against the new source registry bytes would fail the checksum before the job.
This candidate preserves that explicit adoption boundary; Kael must reconcile
installed registry/pin custody separately from Sovrn scheduling.

The seven-request path and protection failures are verified only with injected
transports and clocks. No live credentials, provider reads or state recovery
were used. Every cooperating Sovrn reader must use the same limiter state.
Kael must resolve the scheduler's execution contract before live acquisition.
The smallest concrete option is a separate source-pinned Sovrn-only entrypoint
with a fixed 600-second deadline, explicit live-read flag, manifest verification
before imports, only the closed-week collector and private three-period
coverage receipts. It would give the collector ownership of its isolated
process timer while keeping the shared wrapper's 240-second limit unchanged.
Adding/registering a second scheduled producer and its installation/pin custody
extends beyond this collector ticket; no such entrypoint or schedule is created
here. Suspending or enlarging the shared runner's alarm would bypass its global
budget and is intentionally not an implementation option for this candidate.

Missing contracts for financial completion: authoritative response shape and
revenue semantics; currency/units; calendar timezone; stable ID/revision
ordering; pagination/caps/truncation/total proof; intended per-site binding.
No success count can substitute for these contracts.
