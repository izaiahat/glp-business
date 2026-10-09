# Shared Firecrawl integration contract

Import `scripts/firecrawl_client.py` from maintained producers. REST endpoints are
v2; grouped search response `data.web/news/images` is not a list. Fixtures cover
both web/news and scrapemetadata and failed transports. Caller/run attribution is
append-only under flock in `data/firecrawl/operations.jsonl`, mode 0600; it contains
no URL/query/prompt/contact/auth data. Unknown `creditsUsed` stays null. Started
and terminal entries share operation_id and count as one effect, not two calls.
SDK and MCP receipts are bridged in the customized Hermes fork by
`tools/firecrawl_ledger.py`; GLP_FIRECRAWL_CLIENT can override the installed adapter
path without modifying another profile. Existing sessions need supported MCP
reload; `hermes mcp test firecrawl` verifies 28 tools including government search
on a fresh connection only.

Routine refresh pending-review states do not renew evidence. Preexpiry capture
backs off seven days on an unresolved material review, one day on refused/unknown
outcomes, with exact parent and content hash idempotency. Taxonomy refresh is
due after 30 days (inside the existing 31-day maximum); reviewed corrections
remain eligible immediately. Failed unchanged row/policy work backs off seven
days. Identical URL/options are captured once per taxonomy run, preserving
original capture clocks and guards; memoized refusal never becomes acceptance.

Historical source, unchanged: dated `scripts/g6_r1a_*_20260806.py` (six probes),
`glp-clinic-partner-outreach-machine/*_20260616.py` (two SDK artifacts).
The undated stage5 fetcher is **maintained**, so is migrated. Other-venture
`boring_finder_n8n_run.py`, dated stream2 Tweet research and personal
`x_bookmark_digest.py` are outside the current GLP production fleet/holds and are
not revived by this integration. Sponsor `refill_email_ready_targets.py` and SEO
article evidence integrations are coordinated with their separate assigned owners.

Retrieval authority is not publication or send acceptance. Keep Fella excluded,
provider holds, single-writer/queue/pin gates, maxAge=0 authoritative captures and
historical artifacts. The rewritten live firecrawl-research skill is the routing
owner for indexes/connectors/JSON/monitors/batch/parse/Agent/enrichment and prices.
