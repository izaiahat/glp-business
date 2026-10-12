# DOTS continuous lane (DOT1)

## Interpreter and proof

The operator-authorized dispatcher interpreter is
`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3`, not host pip. The consumer
cron uses the existing Rook venv; both service venvs already contain standardwebhooks.

```
uv pip install --python /home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -r ops/dots/requirements.txt
python3 -m pytest tests/test_dot_lane.py tests/test_dot_dispatch.py tests/test_fleet_ingest.py -q
python3 -B scripts/dot_dispatch.py --consume --max-wait-seconds 1
python3 -B scripts/dot_lane.py ingest
python3 -B scripts/dot_lane.py refill --dot rook
python3 -B scripts/dot_lane.py refill --dot finch
```

Dispatch only through `scripts/dot_dispatch.py --dot rook|finch --issue GLP-N
--brief FILE --owner coordinator`. Do not insert text into a TUI or resend a
pending/uncertain ticket. Native grants permit ONE RESULT/BLOCKED, not ACK.
A terminal native reply is the acknowledgment. Subscription inspection is
read-only; it does not renew subscriptions or reset capacity.

The previously pending `finch-glp-93-551d69f0e8973c0b` was already closed by the
installed no-reply expiry path, with retained EXPIRY.json covering the exact
request. Do not re-open it or fabricate a reply.

## Ingestion and scheduling

Existing `fleet-output-ingestion` job **bbc82551321b** runs
`fleet_ingest_cron.sh`; it is NOT 113e1599dbbb (that is article-image-health).
Its existing deterministic fleet/native consumer refresh is retained; the
wrapper adds DOTS receipt/comment/notification handoff followed by idle refill
in the same existing five-minute cycle. Host cron
`ops/cron.d/glp-dot-refill` is a fifteen-minute backup. Each dot has one in-flight
ticket when safe admitted supply and its exact native subscription are available;
refusal/capacity/empty-supply states are logged, never papered over with fake work.

Evidence lands in `reports/ops/fleet-ingest/<ticket>.json` and
`reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl`. Each PR's URL,
read-only GitHub head SHA and author are queued for coordinator non-author
review. This is not acceptance, issue closure, health-claim review, merge or
release approval. Native custody mismatches refuse before any external write.
Linear and ops-alerts effects are journaled separately. A definitive pre-POST
fleet-scope refusal still writes the genuine native receipt, ops-alert and DOTS
INBOX data as `awaiting_coordinator_comment`; it preserves the exact body for the
coordinator to post. It never switches roles or changes root admission. One dot's
selection-comment hold does not stall the other dot; that dot alone remains held
until exact read-only comment reconciliation succeeds. After an ambiguous
write, the next run does a target readback only; absent effects stay refused for
coordinator reconciliation. Never delete the journal to force another send.

## Supply, not an autonomous approval machine

`ops/dots/lane-policy.json` defines explicit safe scope templates, not another
priority board. Fresh live Linear issues admitted as Todo/In Progress/In Review (never Backlog,
Blocked or Triage) and the coordinator's current PLAN
order determine selection. Already claimed scopes never repeat, including
blocked outcomes. Active issues/files are reserved; a terminal result must be
handed to the coordinator before that dot receives another brief.

Only `public_research` and explicitly file-scoped `independent_pr` scopes are
eligible. Held/protected scopes refuse. No money, credentials, sends,
health-claim acceptance, installs or merge permissions are inferred from a
root issue. The seed supply is new public Ro rail and WW/Noom contact research;
previous cohorts are not repeated. For the allowlisted public-research roots,
a new live acceptance-description digest supplies one bounded evidence-gap
follow-up, never another run of the same unchanged research. The coordinator adds
real safe subtasks as work becomes available; exhausted supply logs
`no_safe_open_scope`, not filler.
Independent-PR files remain reserved in the claim ledger until coordinator
review explicitly releases the scope. Other-owner files must never be added.

## Cloud integration before Rook refill

Before each actual Rook refill, read the three latest exact Rook-authored Cloud /
Tailscale replies, read them back individually, and hand them to GLP-359 and the
same DOTS INBOX. `DOT_CLOUD_UPDATE_V1` explicitly distinguishes this historical
readback from a native ticket completion; it never launches/replays a Cloud task.
PR links carry GitHub head/author and coordinator non-author review, never merge.
The Linear writer uses the installed scoped **fleet** client, not an operator-key
fallback.

R1 owns renewed-key acceptance. Only after R1 writes `cloud_ready_for_dots: true`
in its own RESULT does every brief enable builds/tests/private evidence reads in
`hermes-evidence-account1` for Rook or `hermes-evidence-account2` for Finch.
Current R1 source-only/pending-publication results do not enable this capability.
Do not modify R1's result on its behalf or change keys, ACLs, proxies or grants.

## Manual proof

A fixture is not live proof. Record real Discord request/reply IDs, native
COMPLETION.json, receipt, Linear comment ID, ops-alerts message ID, refill
selection/comment, exact-head CI, merge and scoped zero-drift install in
`reports/ops/fable-plan-2-20261009/lanes/DOT1/RESULT.json`.
