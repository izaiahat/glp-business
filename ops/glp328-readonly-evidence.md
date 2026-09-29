# GLP-328 read-only evidence connection

`glp328_readonly_evidence.plan_current` connects current canonical Sheet values,
existing outreach JSONL/SQLite controls and (only after a successful local
preflight) registered mailbox metadata to the accepted **disabled** planner.
It is a library projection, not another sender, scheduler, queue or contact
source. The accepted `glp328_outreach_policy.py` remains unchanged.

## Authorized host read path

Supply the result of `glp_wire_in_ready_queue.read_live_sheet()` (which reuses
`glp_sheet_sync_drain.read_sheet`) and the existing
`glp_affiliate_inbox_mirror.MAILBOXES` / `gmail_service`:

```python
values = wire.read_live_sheet()
result = evidence.plan_current(values, evidence.LOCAL,
                               mirror.MAILBOXES, mirror.gmail_service)
```

Import the reviewed candidate policy/adapter before loading host dependencies
when executing from an isolated worktree; do not install candidate bytes just
to test this call. Record actual import paths/hashes. No mirror `main`, cursor,
queue method, legacy suppression helper or schema initializer may be invoked.

`read_local_sources` opens `outreach.sqlite` with URI `mode=ro`, asserts the
observed `query_only` state in provenance, and reads targets/suppressions in one
transaction. It reads all five existing JSONL files strictly, preserves the
union of adverse controls, and resolves target identity across both sources.
Legacy display-name addresses are parsed with stdlib `getaddresses`. Only
wildcard prefixes already forbidden by the accepted policy are represented as
`policy_prefix_controls`; unknown wildcard/global controls refuse. Expirations
never clear controls. A missing/malformed/conflicting/unresolved source causes
an incomplete snapshot and a real `plan_outreach` refusal, not a fabricated
empty complete snapshot. API errors expose types, not response bodies.

After local success, both registered mailboxes must be readable. Metadata-only
`messages.list/get` uses all pages and includes spam/trash, with no date cutoff.
Any matching local or Gmail conversation remains active, even an unsent draft.
The source hashes/selected SQLite rows are rechecked after mailbox reads; drift
refuses. Coverage is **official-host query scope**, not a guarantee about
network-mediated aliases or unknown organization domains. Exact known relevant
SENT IDs are checked in ops (owner of partners@), but a label/thread readback is
not an accepted initial-send or follow-up receipt: `verified` remains false.
There is no reply-stop/follow-up authority or activation in this adapter.

## Contacts and limits

Legacy targets, Sheet URLs and mail headers are identity/control references,
not evidenced business contacts or US/provider-service evidence. The optional
`contacts` and `location_evidence` arguments retain the pure planner's existing
caller-supplied evidence contract; this module does not authenticate a new
contact-evidence format. Omitted evidence produces explicit exclusions, never
inferred recipients. Validate already-authorized official source, ownership and
US provider/service evidence separately before supplying it. Do not claim an
empty supplied list is an exhaustive search or absence of real contacts.

A local preflight failure stops downstream mailbox and contact qualification.
Retain the genuine refusal and precise source positions for adjudication; do
not remove a suppression, resurrect a retired target, or run repair/sync code
to obtain a passing result. Fella/holds, forbidden mailboxes, organization
limits and all personal campaign approvals remain the accepted planner's gates.
Prices/templates remain UNAPPROVED. No Gmail drafts or contact sends.

## Focused verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_glp328_readonly_evidence.py
```

Four tests cover local projection/no writes, missing/malformed/unresolved
refusal through the real planner, mailbox pagination/readback completeness,
and source drift. All test records/mail clients are explicit fixtures; they
prove no live routing or sent campaign. Actual host read receipts belong in the
private report directory, not this curated source repository.
