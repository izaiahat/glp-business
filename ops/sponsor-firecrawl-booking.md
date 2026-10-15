# GLP-328: one-shot Firecrawl unknown-cost booking

Authority: `ops/linear/approvals/OPERATOR-DECISION-20261015-firecrawl-unknown-receipts.md`
on the receiving host, exact SHA256
`63b94cd28422b5bb4a5399afbe9854a5f80dbe0d5aa7418f0c7793f6eb1f5b87`.
No dashboard, login, purchase, replay, cap increase or new allowance is needed.
This source change needs independent review and adoption before live `--apply`.

## Supported maintained path

`glp_sponsor_firecrawl_booking.py` uses the existing Producer run and shared
budget locks and the shared operation-ledger flock. It only performs authenticated
GET `team/credit-usage` and `team/credit-usage/historical`; it has no paid transport.
The latter currently returns `creditsUsed`, **not the documentation's
`totalCredits`**. Only whitelisted balance/window fields are retained, never keys.

For every latest logical **paid sponsor** operation with null cost before the
operator decision at 2026-10-15 01:04Z, book **max(1, entire covering authenticated
usage-window delta)**. Historical API windows start with zero cumulative usage;
`creditsUsed` is their account-wide delta to the closed end or readback time.
Giving each operation the entire window total is intentionally an extremely
conservative upper booking, NOT equal-share attribution or exact receipt cost.
No known charges are subtracted and a zero window still books one. Missing,
ambiguous or invalid history refuses. This overbooks historical sponsor-day
accounting; it does not debit the provider or replace authenticated period use.
A tighter per-operation receipt is not claimed. New/future null costs refuse
booking under this one-shot authority; the existing paid null-cost guard stays.

Append-only accounting rows retain original caller, operation ID, operation time,
run, endpoint, URL hashes, HTTP status and **outcome** (including started/ambiguous).
They add a numeric cost and clearly labeled conservative-booking provenance.
Original raw null receipts remain historical evidence; only latest logical paid
sponsor rows have no nulls. Read-only metadata rows and other owners' callers are
excluded. Failed/unknown operations are not made successful or replayable.
No pending reservation is settled or removed, and no cursor/cache is reset.

Before appending, a durable prepared receipt binds reason, approval, authenticated
usage fields/hash, original-row hashes, pre-ledger hash and expected post hash.
`--apply` requires the exact current ledger hash; concurrent changes require a
new plan, never an operation retry. Readback verifies the resulting ledger hash
and zero remaining logical sponsor null costs. Crash after append is reconciled
against the prepared hashes; the provider operations must never be repeated.

```sh
nice -n 19 python3 scripts/glp_sponsor_firecrawl_booking.py \
  --decision /home/ubuntu/business/ops/linear/approvals/OPERATOR-DECISION-20261015-firecrawl-unknown-receipts.md \
  --receipt /home/ubuntu/business/lanes/SPONSOR-SUPPLY/BOOKING-PLAN-NEW.json
# Only after review/adoption, use a distinct receipt path and the plan's hash:
nice -n 19 python3 scripts/glp_sponsor_firecrawl_booking.py \
  --decision /home/ubuntu/business/ops/linear/approvals/OPERATOR-DECISION-20261015-firecrawl-unknown-receipts.md \
  --receipt /home/ubuntu/business/lanes/SPONSOR-SUPPLY/BOOKING-APPLIED-NEW.json \
  --apply --expected-ledger-sha256 <fresh-plan-pre-ledger-hash>
```

Budget policies are untouched: shared 1,500/day, 3,000 reserve, 60,000 only for
the authenticated period ending 2026-10-19T23:59:59Z, otherwise 45,000. No effect
beyond the normal pipeline is authorized: no sends, extra Bouncer or purchase.
Paid admission remains the existing fresh `Producer.reserve` decision; a plan
or sandbox apply is NOT live booking, paid restock or qualified-stock evidence.
