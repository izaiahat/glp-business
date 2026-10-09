# GLP268 remaining caller source handoff

Authority: sole GLP268 owner, isolated source lane; parent retains credential-boundary review and issue closure. SOURCEINSTALLHOLD prohibits live importer, selected-helper installation, retirement and config effects. This is not an activation approval.

## Maintained paths

- `glp_xentraffic_enumerate.main(..., fresh_reader=...)` consumes `xentraffic_fresh_catalog.FreshVisibleCatalogReader` around the caller-owned `FreshAIBrowserReadDriver`. It does not open a browser or load an access file. Its legacy `PortalSession` import name refuses immediately. Visible tables/total/pagers are projected into the existing enumeration call shape; `.api` is a compatibility method, **not** a BFF or supported API request. Missing/duplicate/truncated rows, changing totals, missing published total and unsupported pager layouts refuse instead of claiming complete supply.
- `glp_maxbounty_lane.main(..., fresh_driver=...)` passes the owned driver into `maxbounty.login_and_enumerate`. Exact settings principal and dashboard identity precede search. `maxbounty_fresh_catalog` consumes the maintained `/search` query grammar and fixed visible search/campaign-card projections; no arbitrary JavaScript, private auth API or Playwright context. The original complete-scope and previous-approved reconciliation oracles remain unchanged. Visible category pages do not manufacture the naturally observed approved-account API response or a retired404 receipt; those remain INCOMPLETE/UNKNOWN.
- Read-only replacement routes after release: XT `main(['--queue','off','--heartbeat','0','--read-only','1'], fresh_reader=...)` writes a report but not its mutable baseline; MB `main(['--mode','read-only','--queue','off'], fresh_driver=...)` stops before coupons, application drafts, provider enrichment, payout evaluation, heartbeat or Sheet queue. Both retain the unchanged completeness oracle.
- Shell/bridge calls without the owned driver return structured BLOCKED before profile, Sheet or output I/O. The saved active n8n XT/MB script envelope does **not** carry such a driver. Schedules stay disabled. The transport owner must consume the fresh runtime handoff before claiming pipeline replacement; this PR alone does not do that.
- MaxBounty application/tracking executors have no implicit persistent session factory. Existing legal/provider/image gates remain; a separately admitted fresh operation executor is required. Read-only drivers cannot submit forms, mint links or accept terms.

## API precedence and holds

Keep supported official APIs first. XT's retained account evidence has no publisher key; the visible reader does not substitute its private BFF as a supported API. MB's operation has no supported publisher API in retained evidence. HT's `client_credentials` route remains intact and is not password-grant migration work. Katalys relationship API/fresh reader and PR257 source/install are unchanged. Awin/CJ operation-specific terms/join capabilities remain held where no supported fresh operation implementation is proven.

## Verification boundary

New offline regressions exercise the real normal entry handoffs, no-browser shell refusals, unknown counts, enumeration integrity and fixed read JavaScript compilation. Synthetic UI fixtures are not successful authentication or exact-target effect evidence. Parent must review the exact head before merge, release installation custody, then admit one fresh browser seat and perform registered manual readbacks with the genuine account. Preserve the existing Google device challenge: no resend while operator sleeps, no substitute account secrets. No listener/profile retirement until all enumerated replacement effects are proved; denominator dynamic coverage remains explicitly unknown.
