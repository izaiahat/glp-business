# GLP-42 successor mention transport and exact-request recovery

This is source-only machinery. Merge, guarded source adoption, bridge lifecycle,
Discord sends and reviewer execution each require separate authorization.
Nothing here grants release acceptance.

## New authorized successors

1. Use `scripts/fable_bridge.py --issue-no-decision-successor` with the exact
   predecessor request, issue, tuple and terminal no-decision reply. It issues
   canonical text without sending or invoking a reviewer.
2. Use `ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py
   send-successor --issue-record <issued-record>` for the separately authorized
   single send and bind. It validates the actual outgoing payload before token
   access or POST. Only the configured reviewer is explicitly allowed through
   `allowed_mentions.users`; general parsing stays disabled. It never truncates
   canonical content.
3. The create-exclusive send claim precedes POST and survives uncertain outcomes.
   The authenticated readback must contain the structured reviewer mention, the
   registered bot sender, canonical bytes and unedited normal message before bind.
   Direct successor binds enforce the same eligibility. Never retry a claimed
   send, edit the message, remove a claim or issue a replacement by inference.

`post_discord_message.py` remains a silent notification sender. It now refuses
canonical review requests rather than silently suppressing their mentions.

## Existing BOUND request missing its structured mention

The explicit bridge entry is `--recover-missing-mention REQUEST_ID ISSUE_ID
TUPLE_SHA256`. Do not run it as part of source verification or deployment.

The recovery requires the real authority guard's canonical v2 BOUND issue,
exact message ID, content and content hash, timestamp, author, artifact bytes,
registered predecessor/successor linkage, independent signer and cursor already
at/past that request. Missing/corrupt state, any receipt/claim for the source,
any reply by any author, or incomplete reply history refuses. Normal classification
must reject the original message, while adding only the structured self mention
to a private in-memory copy must produce a design review. The original Discord
message and ordinary mention gate are never modified.

Fresh included session and weekly capacity must both be positive. The route is
pinned to `fable / claude-fable-5-1 / xhigh`; no stand-in or paid fallback. A
persisted, fsynced claim under the daemon's own state lock precedes inference.
All claims are nonretryable, including crashes before invocation and ambiguous
inference, post or attachment verification. The cursor is never rewound.

Canonical bindings, artifact, routing and complete reply history are rechecked
after capacity collection, immediately before each invocation and before posting.
The existing PR #23 delivery path is reused: a long initial output may receive
one compact follow-up in the same reviewer session and is attached verbatim to
one nonce-protected reply, with exact attachment byte/hash readback. This is one
recovery transaction, not permission to retry the request or switch reviewers.
A failed compact follow-up can only produce a no-decision reply, not acceptance.

## Verification and adoption

`tests/test_fable_mention_recovery.py` uses real canonical issue/bind/load code,
normal classification, review/invoke/post and attachment verification with only
network, CLI and filesystem roots isolated. Existing successor tests now run the
curated real guard in CI, not a host-only skip. No fixture invokes a live reviewer.

The first PR commit preserves exact predecessor bytes for the newly curated
notification sender, authority guard and nonsecret authority config. Future
source adoption must explicitly name those three paths to the existing
`--adopt-reviewed-baseline` guard if absent from sync state. Preview the complete
manifest against current live bytes; unrelated drift still refuses. Do not
manually copy source, overwrite runtime state or restart PID 170239 under this PR.

## GLP73 fixed current-release designation (source only)

The guard's new-issue path recognizes only an explicit top-level
`reviewer_designation: "glp42-pr8-astra-20260926"` in a
`glp.q44.independent-review.v1` packet with status
`READY_FOR_INDEPENDENT_REVIEW_NOT_ACCEPTED`. The existing `issue` CLI and
issue/bind/readback path remain the producer; do not edit issued requests.

The packet's recomputed canonical `tuple_sha256` must match the issue argument.
Its tuple must contain every exact `GLP42_REVIEW_SUBJECT` field in the maintained
guard: subject `glp:GLP-42:pr8:38133df58fc5406adeb87ed88771cc6ceeb26342`, that site
head, the frozen final381 root and closure, current public-tree and deployment
manifest hashes, deployment ID, rollback target and retained authority hash.
The fixed operator-decision file must still have SHA256
`7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0`.
This is the already-frozen PR8 release used by GLP73, **not** PR8's subsequently
advanced branch head, a new release, or a wildcard for future heads.

The guard adds the canonical designation only to new opted-in issues. Historical
v1/v2 reconstruction without that field is byte-preserving. The bridge requires
the real guard-owned BOUND record plus authenticated exact request readback,
structured mention, unedited author/channel/content/timestamp and message ID;
a copied marker or replay under another message cannot acquire the route.
Authority and bound artifact are rechecked before each inference. Generic
outward-design classification, Fable quota routing, genuine TOP_STAKES deferral,
reply identity, guard acceptance and ordinary daemon no-replay remain unchanged.

The selected backend is `astra-glp42 / openai-codex / gpt-6-astra-900k / xhigh`.
It reuses the existing fresh named Astra session and native read-only reviewer
entry. Standard is configured **process-locally**, with cross-provider/model
fallback disabled; no saved config or quota state changes. Receipts record the
actual command's backend/model/effort/provider, named session, configured speed,
and invocation outcome. `service_tier_config=default` means normal/no priority
override in the installed classic CLI; it is not provider-returned wire evidence.
Runtime adoption must verify the selected session and effective route separately.

Routing eligibility is **not acceptance** and does not prove the evidence packet
complete. The independent non-author reviewer still owes the full gate receipt,
renders, consent/default-on/GA4 disclosures, exact package, rollback and all
current suite disclosures. The fixture packet deliberately is not a complete
review packet. No acceptance is transferred from another artifact. Parent
independent source review, guarded source adoption, receiving-runtime readback,
a fresh complete packet and separately authorized issue/send/reviewer execution
remain required. No source installation, bridge invocation or send is performed
by these tests.
