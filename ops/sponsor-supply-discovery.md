# Sponsor discovery producer (GLP-328)

## Finding and source boundary

`ops/hermes-cron-scripts/glp_sponsor_firecrawl_supply.sh` invokes
`Producer.daily()` in `scripts/glp_sponsor_firecrawl_supply.py`.
Previously its entire input universe was `SPONSOR_SEEDS`, current plan gaps,
qualified provider-discovery files and four fixed October 9 retained-search
JSONLs. There was **no fresh sponsor search/category producer and no search
cursor to advance**. Known organization/capture/alias exclusions correctly
prevent replay; removing them would recycle contacted organizations, not add
supply. The approved category predicates already cover nutrition/protein,
meals, fitness, CGM/metabolic health, weight coaching, GLP telehealth and
pharmacies. They are unchanged. General supplements and labs are NOT newly
admitted categories.

The retained run `sponsor-supply-20261014T2342` had 181 research hosts,
155 with retained pages, 7 untouched retained hosts, 36 recipients,
33 conversation/reply/bounce refusals, 3 mail-admitted, **0 verified unsent**,
and 0 Bouncer calls. This is exhausted input, not an email-verifier outage.

## Maintained repair

`glp_sponsor_discovery.Discovery` runs inside that same daily producer:

* Free Wikimedia category enumeration, continued page-by-page, and literal
  Wikidata P856 official websites. Six category indexes are research sources,
  not first-party evidence, US evidence, organizations, contacts or stock.
  One literal HTTPS official URL per entity; foreign regional franchise URLs,
  insecure URLs and tracking links are refused. No inferred HTTPS upgrades.
  Wikimedia structured data is openly licensed (Wikidata CC0). Bing RSS was
  investigated and rejected: its feed explicitly restricts commercial reuse.
* 714 deterministic category/geography/purpose queries across all seven
  **existing** categories. At most four Firecrawl web-only searches of 100
  hits per daily run, no scrape options. Each reserves 20 credits via the
  existing `Producer.paid()` fence (80/day maximum for discovery, NOT an
  additional allowance). Existing daily 1,500, period cap and 3,000 reserve,
  shared lock, unknown-credit refusal and pending reservations still apply.
* Durable receipts/backlog are stored under the existing sponsor runtime's
  `sponsor-discovery/`. Discovery observation never consumes a host as 'seen'.
  New hosts precede old seeds/cache in capture selection; registry, named
  aliases, suppression, conversation and Fella exclusions remain applied.
  Search still uses `firstparty_research_host`; directory links are only
  literal research targets. Unchanged capture/merge/US/category/role/legal,
  exact public mailbox, mail-admission and Bouncer gates remain downstream.
* Cursor advances only with persisted response/backlog. Paid attempts are
  marked pending before transport; timeout/interruption requires reconciliation,
  not replay. Admission refusal is held for the UTC day, without cursor loss.
  Exhausted free directories/query packs report exhaustion, never reset to
  pretend new supply. Default free enumeration is two batches/day. Wrapper
  runs `nice -n 19`. No new cron, queue, sender or auto-approval is introduced.

The null-cost 403 receipt `730f87ff-ce94-4894-bdaf-b1a927d3e503` and three
October 13 unknown operations remain unreconciled. **No paid search/capture,
Bouncer, mailbox call, send, purchase, install or merge was executed for this
change.** Dashboard-login reconciliation remains operator-owned. A later UTC
date is not authority to disregard unknown period reservations.

## Yield forecast — uncertain, not stock acceptance

A real, sandboxed, free-only run processed all six indexes: **9 category
batches / 143 indexed articles / 66 literal official-site hosts / 58 previously
unseen research hosts** after comparison with retained organization/capture
identities and named aliases. This is not live suppression or US/category
qualification. Evidence: `lanes/SPONSOR-SUPPLY/{FREE-FINAL-1,FREE-FINAL-2,
YIELD-EVIDENCE}.json` and `free-final/receipts/` in the host workspace.
At the default two batches/day that finite bootstrap averages about **13
new research hosts/day for roughly five runs**, then zero from these indexes.
No directory contact yield has been measured: use **0–3 verified contacts/day
as a planning scenario only**, not a confidence interval or empirical rate.

After the paid refusal is genuinely reconciled, 4 x 100 search hits/day are
an upper bound, not 400 new hosts. A **40–140 new hosts/day** scenario assumes
10–35% survive duplicate/own-offer/identity filtering; this percentage is
unmeasured. Historical 36/181 = 19.9% gross-recipient yield is an optimistic,
non-comparable ceiling, while the actual exhausted-pool net yield is 0%.
Using an explicitly hypothetical 5–20% end-to-end yield gives **2–28 verified
contacts/day**. Eligibility/cost failures can still make the real yield zero.
25/day needs 125 fresh hosts/day at 20%, or 500/day at 5%; neither is proved.
75 stock requires 3–38 no-send days at 2–28/day. At <=25/day restocking cannot
grow stock while sending 25/day. Do not declare the business target met.

At 45,939 used against the temporary 60,000 period cap, gross headroom was
14,061; 103 pending was also retained. At the full 1,500/day cap that supports
at most nine whole additional run budgets before this cap expires October 19;
calendar time, shared spend and unknown receipts can reduce it further.
The default reverts to 45,000 for other authenticated periods. No forecast
assumes a purchase, higher cap, relaxed mailbox rules or automatic renewal.

## Review/adoption

RED→GREEN tests cover missing daily discovery, real candidate reachability,
shared paid-fence refusal, directory continuation, alias compression,
HTTPS-only literal URLs, pending-operation no-replay, receipt failure and
backlog persistence. Public probe exercises maintained source but is NOT an
installation or qualified-stock receipt. One PR, exact-head CI; distinct
review and separately authorized installation are mandatory. Do not self-merge.
