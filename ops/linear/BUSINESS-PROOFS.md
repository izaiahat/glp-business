# Read-only business proofs (GLP-358)

`python3 -B scripts/linear_business_proof.py GLP-n` prints JSON: PASS/0 or
REFUSED/3. Missing output, incompatible schema, unknown financial coverage and
changed acceptance refuse. No sends, callbacks, tracking requests, publication,
pruning, dispatch, scheduler changes or Linear writes are available.

## Gates and receiving

The predicate fetches current Linear Acceptance and compares every clause with
PLAN. Whitespace is normalized; the obsolete explanatory `Read-only proof:`
suffix is ignored. GLP-93 now binds the owner's later authorized, multi-clause
69-row contract, not the superseded ten-program threshold. Its frozen rows are
not treated as 69 distinct programs.

Proof PASS does not authenticate independent review or authorize closure. The
existing fleet completion preflight and protected validators remain mandatory.
A worker/reviewer ACCEPT cannot replace business output. The candidate policy
must be received into the registered fleet plan after independent Astra review;
this PR does not install source, change registered policy, or enable jobs.

Fleet accepts only the exact new argv shape and fourteen reviewed issue IDs,
requires the proof issue to match the ingested issue, uses the declared canonical
cwd and bounded integer timeout, and fingerprints the new verifier bytes. Shell
text, extra flags and unknown issues refuse. Existing proof contracts remain.

## Output bindings

A worker may supply `business_output`, a dictionary of named native-output
locators. Each locator is `{ "path": "reports/.../native.json", "sha256": "..." }`.
These must point to real maintained producer outputs with the fields consumed by
the issue predicate. They are not a license to create a normalized success report
or copy fixture data. Producer-schema mismatch refuses and requires an adapter
repair/review, not hand-written completion assertions. Path prefixes, symlinks,
changed bytes, review filenames and worker status are checked separately.

Direct readers cover exact Gmail threads/messages, canonical static URLs without
redirects, Sheet reads, fixed SELECT-only receiver/D1 queries, provider email
GETs, exact GitHub merge/head/CI, Discord result GETs, scheduler registry, filesystem
free bytes and state.db size. Existing native runtime helpers and account bindings
are deployment prerequisites; offline CI never authenticates or exercises them.

Native artifacts additionally carry issue-specific release/render/notifier,
sponsor-pilot/stop, prune/scheduled-cycle, 24-hour admission, lineage/reversal,
registry/queue/Sheet, closure/idempotency and Cloud execution outputs. Missing
locators are an explicit refusal, not a passing prerequisite. Success controls
in tests are synthetic and must never become runtime evidence.

## Worker repairs and verification

GLP-320 binds the actual GLP320-LIVE worker, not SEO-ACCEPT. GLP-359 binds the
existing DOT-OWNER worker. GLP-88/250/253 have explicit issue-bound noncomplete
records instead of nonexistent L8 paths. GLP-125 has a new issue-bound fresh live
readback; its historical receipts are not modified and the reply must not resend.

Run the six focused test modules named in the fleet-ingest workflow. Hosted CI
checks out the exact PR head. Live candidate exercise and per-issue observations
are under `lanes/CLOSURE-LOOP/proof-verifiers-20261016/`; the harness resolves only
new policy records from the isolated checkout and reads all business targets live.
It does not install unreviewed code or run the effectful poller/closer.
