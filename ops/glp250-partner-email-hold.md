# GLP-250 partner-email hold contract

Source-only. Independent review, merge, guarded source adoption and separately
authorized single-writer apply are still required. No inbox disposition, provider
activation, network relationship change or hold release is performed by this mode.

## Maintained route

`sheet_update_queue.kind=partner_email_hold` →
`glp_sheet_sync_drain.build_entry` →
`affiliate_sheet_batch_writer.SHEET_SYNC_KIND_MODE` →
`run_sheet_sync_mode`. The existing drain CLI owns the writer lock. The writer
retains whole-Sheet CAS, readback, collateral detection and idempotent replay.
The existing queue duplicate-consistency check also covers this exact kind.
No adapter, store, arbitrary instruction interpreter or generic email classifier
is introduced. The approval reconciler's existing
`drain_single_writer_queue_row(fact, False)` remains the later enqueue/drain route;
its `True` argument is only an echo, **not** an actual writer preview.

The sole supported factual interpretation is the already-recorded XenTraffic
3277 advertiser-clearance/postback-readback hold. Production source pins only
nonsecret event ID, original raw digest and received timestamp. Original raw mail
remains in the private inbox store; no body, sender address or tracker is curated.
Every writer check reads the exact inbox record with SQLite URI `mode=ro` plus
`PRAGMA query_only=ON`, rehashes its retained bytes and verifies the expected sender
domain and aligned Google DMARC result. Caller-supplied email text or an
`authenticated=true` field cannot authorize a hold. Another email/offer needs a
separate reviewed contract, not a new row in an arbitrary authority file.

## Queue fact

Prepare exactly one private JSON-array fact, with these fields:

- `kind`: `partner_email_hold`; `status`: `queued`.
- `network`: `XenTraffic`; `program_id`: `3277`.
- `program_name`: exact current Sheet program name; `sheet_row`: resolved integer
  row (not identity authority). Canonical network + exact offer Apply URL must
  identify exactly one existing row, even when an explicit row number is supplied.
- `update_id`: `partner-email-hold-` + exact event ID + `-` + exact raw SHA256.
- `evidence.event_id`, `.raw_sha256`, `.received_at`: the immutable production pins
  in `PARTNER_EMAIL_HOLD_EVENT`, independently matched to the retained event.
- `evidence.corrected_at_utc`: fixed UTC planning/correction timestamp, later than
  the historical email and not future-dated. Preserve it on replay.
- `evidence.source_row`: the entire 27-cell, unformatted, typed preimage from a
  fresh canonical Sheet read. Preserve it on replay; never substitute the postimage.

Only Status (zero-based column 10), Wire-In Ready (19, FALSE only), and
Notes/Blocker (23) can change. Status is fixed to
`hold_advertiser_postback_pending`. The generated note appends to the complete old
note, without stripping whitespace or truncating provenance. It identifies email
receipt time separately from metadata correction time and does not assert a fresh
portal observation. Last Checked UTC, Wired Live, Fit Reason, tracker, payout and
every other cell are unchanged; the appended note explicitly supersedes the old
readiness/no-per-offer-gate claims for this hold. Network eligibility is UNKNOWN,
not paused, withdrawn, rejected or zero-economics.

The captured source must be tracking-ready with a tracker, Wire-In Ready TRUE and
Wired Live FALSE. Every read, including the immediate pre-write check and replay,
requires the complete source preimage **or** complete deterministic postimage.
Partial postimages and unrelated-cell drift refuse. Free-form queue notes/cells
are not applied. No creation path exists.

## Exact future preview command

After independent source review and authorized guarded adoption, first prepare the
one private fact above at the following path (it is intentionally NOT created by
the source-only implementation). Run from `/home/ubuntu/business`:

```sh
python3 scripts/glp_sheet_sync_drain.py \
  --queue-file reports/ops/glp250-email-hold-contract-20260927/private-hold-fact.json \
  --apply false \
  --output reports/ops/glp250-email-hold-contract-20260927/future-preview.json
```

This is the actual registered drain preview, not an enqueue and not the
reconciler helper's dry-run echo. It reads the Sheet and writes private plan/report
files only. Require one result with `reason=dry_run`, `planned_cells=3`, no refusal,
and `cells_written=0`; envelope `PASS` alone is insufficient. A later apply needs
fresh source/custody readback and separate approval. Preserve the same fact and
idempotency identity; do not refresh the note timestamp to force a new write.

## Curated portability and test scope

The drain and its two missing writer import prerequisites
(`affiliate_program_relevance.py`, `affiliate_operator_rulings.py`) were curated
as exact, secret-screened maintained predecessors before the repair commit. No
private runtime dependencies or data were copied. Their predecessor hashes are in
the Git manifest; later adoption must use the existing reviewed-baseline policy
for newly curated paths, not overwrite unrecognized live bytes.

`python3 -m pytest -q tests/test_glp250_partner_email_hold.py` exercises the real
registered drain, writer mode, full pre/postimage guards, CAS, fixture batch writes,
readback and replay. Only Google transport/OAuth and the unchanged global Sheet
schema audit are fixture boundaries. SQLite writes are confined to clearly
synthetic fixture setup; the production reader is read-only. This proves source
behavior, not current live Sheet freshness, transport schema, live queue completion
or a successful hold correction.
