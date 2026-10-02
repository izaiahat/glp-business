# Combined F07 / GLP-333 / GLP-44 identity publication

`f07-identity-publication-20261002` is the corrected fixed 16-member cohort for
hourly collector and weekly reporting publication. It binds committed source
`87f681fa7e011a3f6af132ee7a4e27d6778ebbdf`, including PR151/153 and the exact
weekly identity-pin repair. All earlier generation contracts remain immutable.

The receiving review refused PR157 because its wrapper checked identity digest
`93aa4fa8…` while publishing registry `0dc42e33…`. The corrected wrapper digest is
`58653a04c0c331bee1f1a6f43b5da865b02a22593a9edb762914bc2c326a316f`.
Its only payload difference from PR157 is that identity-pin literal. The fixed
adopter publishes all 16 members, out-of-tree wrapper and weekly job hash in
one maintained transaction. No guard, inventory scope, review requirement or
scheduler behavior is changed; no generic ship tool is required.

## Supported receiving path

From the final reviewed clean checkout:

```bash
python3 -B "$CHECKOUT/scripts/glp44_scoped_adoption.py" \
  --generation f07-identity-publication-20261002 \
  --checkout "$CHECKOUT" --root /home/ubuntu/business \
  --hermes /home/ubuntu/.hermes
```

Outside-window preview is diagnostic only. After source review, coordinate the
serialized install turn and direct-reader/writer exclusion, enter a fresh
maintained maintenance window, and add `--window "$ACTIVE_JOURNAL"` to preview.
Obtain a fresh independent exact-proposal review (outer schema unchanged,
`approved_source` as above; runtime schema
`f07-identity-publication-runtime-review-v1`). Then the same CLI with
`--review "$REVIEW" --apply` publishes exactly once. Preserve custody and native
registry readback, restore via maintained exit and verify complete closure
before the registered hourly and weekly runs. An uncertain save is a
reconciliation hold, not replay permission. Never reuse the refused PR157 receipt.

The six new leaf modules were separately installed by the maintained scoped
importer before the refused window, which was subsequently closed. Authenticate
fresh preimages; do not repeat that import. Any genuinely absent member still
requires separate custody: the unchanged missing-predecessor guard remains.

## Regression boundaries

`test_f07_registered_weekly_entry.py` executes shell → unchanged pinned Python
job → unchanged pinned reporter → real `weekly_collectors` function entry,
stopping there before credentials/transport/report effects. Portable CI only
relocates absolute roots and rehashes the relocated manifest/synthetic roster;
identity bytes and the identity pin are never substituted. The predecessor-pin
control exits 66 before Python. Fixture bytes/mtimes remain unchanged. Receiving
review separately authenticates all four shell pins including the private roster.

PR119 fixture history retains the current producer with historical payloads so
its exact-byte/adoption/rollback controls continue to run. Intermediate fixture
commits are not deployment candidates; use only the final reviewed head.
