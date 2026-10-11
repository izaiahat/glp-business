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
wrapper adds DOTS receipt/comment/notification handoff. Host cron
`ops/cron.d/glp-dot-refill` runs every 15 minutes and dispatches only idle dots.

Evidence lands in `reports/ops/fleet-ingest/<ticket>.json` and
`reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl`. Each PR's URL,
read-only GitHub head SHA and author are queued for coordinator non-author
review. This is not acceptance, issue closure, health-claim review, merge or
release approval. Native custody mismatches refuse before any external write.
Linear and ops-alerts effects are journaled separately. After an ambiguous
write, the next run does a target readback only; absent effects stay refused for
coordinator reconciliation. Never delete the journal to force another send.

## Supply, not an autonomous approval machine

`ops/dots/lane-policy.json` defines explicit safe scope templates, not another
priority board. Fresh live Linear issues and the coordinator's current PLAN
order determine selection. Already claimed scopes never repeat, including
blocked outcomes. Active issues/files are reserved; a terminal result must be
handed to the coordinator before that dot receives another brief.

Only `public_research` and explicitly file-scoped `independent_pr` scopes are
eligible. Held/protected scopes refuse. No money, credentials, sends,
health-claim acceptance, installs or merge permissions are inferred from a
root issue. The seed supply is new public Ro rail and WW/Noom contact research;
previous cohorts are not repeated. The coordinator adds real safe subtasks as
work becomes available; exhausted supply logs `no_safe_open_scope`, not filler.
Independent-PR files remain reserved in the claim ledger until coordinator
review explicitly releases the scope. Other-owner files must never be added.

## Manual proof

A fixture is not live proof. Record real Discord request/reply IDs, native
COMPLETION.json, receipt, Linear comment ID, ops-alerts message ID, refill
selection/comment, exact-head CI, merge and scoped zero-drift install in
`reports/ops/fable-plan-2-20261009/lanes/DOT1/RESULT.json`.
