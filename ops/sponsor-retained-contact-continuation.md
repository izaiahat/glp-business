# GLP-328 retained contact-page continuation

## Actual zero, not a verifier defect

The installed normal verification path had 36 recipients, three mail-admitted
addresses and zero verification calls. All three had fresh negative Bouncer
observations (two undeliverable, one risky). Readiness correctly blocked them.
The 18 new cache hosts supplied 31 email hits: 30 non-business locals and one
invalid shape. No mailbox in that group qualified. This change does not touch
verification, contact-role eligibility, mailbox/suppression admission, rates,
copy, approval, category eligibility, or the financial oracle.

## Reproduced producer defect

`Producer.capture_host()` returned an entire host cache for 25 days before
examining literal navigation links in those retained pages or the producer's
existing first-party discovery captures. A host already captured is not a
fully explored partnership-page set. Supplemental retained US-service pages
also do not necessarily live in the host-cache directory.

A failing regression invokes the actual `capture_host()` with a current,
hash-bound US GLP telehealth page containing an explicit affiliate link. Before
the repair it makes zero captures of that unvisited link. After the repair it
captures only that new literal page, leaves the old bytes/timestamp intact,
and never maps or re-scrapes the homepage. The same route is exercised from
`daily()`, not a separate planner or scheduler.

## Narrow remedy

The maintained producer now considers current, hash-bound first-party cache
and discovery pages together. It reuses the unchanged full-page category and
US evidence validator, and additionally requires explicit own-service or
shipping scope rather than accepting a street address as US service proof.
Existing public business or professional contacts stop this continuation,
including contacts with negative verification; they are not enrichment retries.

Only literal HTTPS partnership/affiliate navigation routes (including explicit
first-party affiliate subdomain roots) can continue. External, query/tracking,
fragment, contact/support, article routes and malformed URLs are refused.
Relative links honor the HTML base URL. No domain, address, path or US service
scope is invented. Both retained requested/final URLs and **every exact prior
scrape attempt** in the shared operation ledger are consumed: successful,
failed, pending, 408 and unknown-cost targets do not replay.

The continuation consumes the existing per-host page bound, the same
`paid()` reservation/fresh-admission fence and flat rawHtml/basic/parsers=[]
request shape. No map/homepage charge is added. The financial oracle, booking
categories, period scope, 1,500/day and 3,000 reserve are untouched. Regression
coverage feeds an HTTP408/null-cost operation into the actual paid fence and
proves zero transport, no refund/ledger edit and no reservation write.

## Retained-source yield and limits

The effects-denied probe freezes the 349-cache-host snapshot, persists batches
of ten, re-aggregates the saved batches, validates retained scope and invokes
the actual daily selector with retained registry/local inputs and all effects
intercepted. It identifies two previously unattempted research pages:

- `https://shop.modernwellnessclinic.com/affiliate`
- `https://affiliates.proteinwise.com/`

Both organizations have retained first-party own-US-service/shipping and
existing-category evidence, and both remain selected by the actual daily
selector with the retained registry snapshot. These are **two contact-page
research targets, zero eligible email contacts and zero new verified stock**.
A form/portal, recruitment-only address, support mailbox or later scope failure
must remain a refusal. No claim is made that these two pages will yield a
mailbox or meet 25/day or 75-stock acceptance. The registry snapshot is retained
October 3 data; it is not a fresh Sheet/mailbox admission readback.

Host evidence (not committed private source):
`lanes/SPONSOR-SUPPLY/CONTACT-{CACHE-SNAPSHOT,CONTINUATION-COHORT}.json`,
`CONTACT-CONTINUATION-BATCHES.jsonl`, `contact_continuation_probe.py`,
`CONTACT-CONTINUATION-RED.txt`, `CONTACT-URL-RED.txt`, `CONTACT-BASE-RED.txt`,
and `CONTACT-CONTINUATION-GREEN.txt`.

## Receiving boundary

This is a sourcewriter PR, not installed or business Done. Distinct parent
review, separately authorized receiving/install and the registered actual
runtime readback remain required. **Do not execute these paid targets until
the exact existing HTTP408/null-cost hold is reconciled through the supported,
reviewed API-only command**, with fresh authenticated allowance proving the
60,000 cap applies to its exact period (ending 2026-10-19T23:59:59Z), 1,500/day,
3,000 reserve and every durable pending reservation. Unknown charged is not
zero. No replay or new conservative booking category is authorized here.
There is no dashboard-login request, paid/provider call, Bouncer retry,
mailbox call, send, Sheet write, schedule change, merge, install, production
source change or approval change in this sourcewriter phase. The enabled
09:00 PDT schedule is not a delivery receipt. Parent retains closure authority.
