# GLP-327/328 exact interrupted sponsor receipt recovery

Source-only change. Astra acceptance and authorized installation are prerequisites
for live accounting adoption; this document is not merge/install/send authority.

## Scope

Only local operation `ad3ae711-d49a-42fa-b332-a6ce60ee376c`, registered
job `ad09c2344709`, run `sponsor-supply-20261016T2350`. Do not pass the
local operation UUID to Firecrawl as if it were a provider scrape ID.
The installed historical conservative booking cutoff and exact Oct15 operation
`224ef323-f3b6-4780-8553-fbf191c3c598` are unchanged.

Read `/v2/team/activity?timeRange=24h&endpoint=scrape&limit=100`, exhausting
pagination (at most ten pages). Match exactly one activity target hash and exactly
one local logical operation for that URL in the retained interval. Then GET
`/v2/scrape/{provider-id}` and require the returned scrape ID and source URL to
match, with a finite nonnegative numeric `metadata.creditsUsed`. Provider activity
returns naive date strings; this tool does **not** infer a timezone or use those
strings to break a collision. Missing/expired/ambiguous evidence is UNKNOWN,
not zero, a historical account-wide usage allocation, or permission to replay.

## Reviewed adoption procedure

After source acceptance/installation, in a new private evidence directory:

```
python3 scripts/glp_sponsor_receipt_reconcile.py --receipt /absolute/private/new-plan.json
python3 scripts/glp_sponsor_receipt_reconcile.py --receipt /absolute/private/new-apply.json \
  --apply --expected-ledger-sha256 PLAN_PRE_LEDGER_SHA \
  --expected-evidence-sha256 PLAN_EVIDENCE_SHA
```

All HTTP operations are GET; the audit and provider responses are separate
exclusive mode-0600 receipt artifacts, not writes to the financial CAS target.
The producer run and shared budget locks precede reads. An apply re-fetches
provider evidence, requires the exact full-ledger preimage, prepares durable
pre/post evidence, and appends one cost-only revision. Any concurrent ledger
write, including read-only account metadata, invalidates the plan. Existing
bookings, original outcome, options, run, timestamps and reservations stay intact.
A completed append is idempotently recognized without another provider read.
A prepared receipt is not proof of adoption: compare the retained pre/post hashes
and exact latest logical row under the maintained locks; use a fresh receipt for
a subsequent invocation. Never remove a consumed namespace or replay a scrape.

The interrupted execution may be terminal `unknown` while its exact API cost is
known. Do not call `finish_execution` as a different process, rewrite ownership,
or mark business completion: the scheduler's supported abandoned-owner recovery
already terminalized execution `346da4d07116441ba7b9e6bfd555a316` at
`2026-10-16T23:59:30.821271Z`. Read it back. Cost recovery does not adopt scraped
content, settle pending reservations, change cron configuration, start supply,
spend Bouncer credits, or send mail. The existing started-outcome capture fence
continues to prohibit replay after successful cost booking.

After authorized live adoption, read the actual accounting guard and run a fresh
cap-zero census. Additional registered refills require the current explicit
bounded-supply authority, new namespaces and all unchanged period/daily/reserve,
public-first-party role-address, Bouncer, consent/suppression and provider holds.
Fella remains excluded. No sends are authorized here.
