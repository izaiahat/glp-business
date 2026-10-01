# GLP330 retained discovery predecessor migration (source only)

This is a one-preimage exception to the curated importer's **ancestry** gate,
not `--force`, a new baseline, fabricated Git history, or approval to apply.
Only authentic SHA256
`088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf`
at `scripts/affiliate_supply_discovery.py` qualifies, and only the accepted
PR111 merge `f6f5704157bc988d406732eb040ac21d343f82fa` discovery bytes
`8f882b3fc6ae1d6fc81590c94c84b1e28c1f094e273b7b0643c013366682ca7f`
can replace them. The retained fixture is those authentic original bytes, never
an executable downgrade staged at the production path. No discovery baseline
may already exist, even if it names the known preimage.

## Why the existing routes cannot admit this file

`--adopt-reviewed-baseline` correctly requires a prior committed revision at the
same path. The retained pre-GLP96 producer was uncommitted, and intentionally
remains absent from that ancestry. `glp73_sync_rebaseline.py` only recognizes
its fixed Rook drift manifest. Paired/scoped GLP73/GLP44 adopters have fixed,
unrelated scopes. None is a truthful supported route for this exact discovery
preimage. The new guarded handoff reuses ordinary scoped sync, source manifest
checks, destination byte/mode CAS, and `maxbounty_pin_release.py`'s strict JSON,
regular-file, custody and preimage-readback helpers; it changes no scheduler.

## Required later inputs (NOT supplied approval)

Use an immutable clean local checkout of the **independently reviewed installer
head**, with PR111 history available. Execute that checkout's importer, not the
old live importer. The selected set must contain discovery and may contain only
these five coupled discovery inputs:

- `scripts/affiliate_monday_discovery_card.py`
- `scripts/affiliate_monday_marketplace_receipt.py`
- `scripts/affiliate_supply_expansion.py`
- `ops/hermes-cron-scripts/affiliate_supply_discovery_daily.sh`
- `tests/test_affiliate_discovery_resume.py`

All selected successor bytes must equal PR111. Review and authority are separate
regular-file JSON documents; neither should be committed to disguise later
operator approval. See the task `RESULT.json` for observed exact hashes and a
non-authorizing proposal template. Re-read state and all selected preimages
before review, and bind the installed SHA256 **and integer POSIX mode** (or null
for a missing selected file). Their proposal must match exactly:

```json
{
  "installer_head": "EXACT_REVIEWED_SOURCE_PR_HEAD_OR_SEPARATELY_REVIEWED_MERGE_HEAD",
  "source_head": "f6f5704157bc988d406732eb040ac21d343f82fa",
  "root": "/home/ubuntu/business",
  "selected": ["SORTED_EXPLICIT_PATHS"],
  "predecessor_sha256": "088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf",
  "successor_sha256": "8f882b3fc6ae1d6fc81590c94c84b1e28c1f094e273b7b0643c013366682ca7f",
  "state_before_sha256": "FRESH_EXACT_STATE_FILE_SHA256",
  "installed_manifest_before": {"sha256": "FRESH_INSTALLED_GLOBAL_MANIFEST_SHA256", "mode": 420},
  "installed_before": {"scripts/affiliate_supply_discovery.py": {"sha256": "088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf", "mode": 420}}
}
```

Independent review document: schema `glp330-discovery-migration-review-v1`,
decision `ACCEPT_EXACT_MIGRATION`, nonempty `prepared_by` and distinct nonempty
`reviewer`, and the exact `proposal`. This is acceptance of the **changed
installer contract and exact successor**, not reused main-merge authority.
Separate adoption document: schema `glp330-discovery-adoption-authority-v1`,
decision `AUTHORIZE_EXACT_MIGRATION`, nonempty `owner`, SHA256 of exact
review document bytes in `review_sha256`, the identical `proposal`, and a
nonempty `writer_exclusion` identifying the owner's actual exclusion of all
consuming writers/readers. Names and JSON are not cryptographic authentication;
the authorized parent must validate the real review and authority provenance.
No source-author acceptance or receipt synthesis is permitted.

Apply requires existing regular consumer lock files, held exclusively
and nonblocking by this path, in this order:
`locks/glp-affiliate-sheet.lock`,
`reports/glp-clinic-scout/affiliate-machine/applications/.application-machine.lock`, and
`../.hermes/state/affiliate-supply-discovery.lock`. As in the existing importer,
cooperating locks do not stop uncooperative writers. Active process exclusion is
the owner's separate requirement; receipt text does not magically enforce it.
Do not create, pause or change jobs just to satisfy this source change.

Use explicit `--only` for each selected path, the existing reviewed-ancestry
flags only for other qualifying paths, and both
`--discovery-predecessor-review REVIEW.json` and
`--discovery-adoption-authority AUTHORITY.json`. A missing or moved input,
changed head/preimage/mode/state, unrelated successor, symlink, wrong receipt,
unknown scope or busy consumer lock refuses before custody/source/state writes.
Under apply, all retained bytes/modes, receipts and state are preserved in
`reports/ops/pin-archive/glp330-discovery/<review-sha256>`, with ordinary importer CAS
and rechecks before replacement. The predecessor and global manifest remain in
custody; scoped sync leaves the installed global manifest and other baselines
unchanged. This is not a multi-file transaction: a late failure after a prior
selected replacement can leave a partial import with custody. Reconcile exact
readback before any retry; do not regenerate receipts from changed bytes or use
the special migration twice. After successful apply, remove the two migration
flags and verify ordinary scoped dry-run `diffs=0` against the same head.

The offline fixture uses the authentic retained body at its original relative
path and synthetic independent receipts. It proves migration, refusal and CAS,
not production review, source installation, scheduled discovery or remote effects.
