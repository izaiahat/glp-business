# Persistent Linear → Pro route

For each admitted Linear issue, prepare a concise read-only advisory prompt with the current acceptance, exact relevant source/evidence, and a decision the execution owner can use. This route does not give Pro implementation, release, spend, outreach, or issue-mutation authority.

```sh
python3 scripts/pro_route.py submit --issue GLP-73 --prompt-file /absolute/prompt.md
python3 scripts/pro_route.py status
python3 scripts/pro_route.py fetch --job <returned-job>
```

Until this PR is independently reviewed, merged and installed, the authorized proof route lives at `/home/ubuntu/business/pro-route-20261007/scripts/pro_route.py`; no replacement of the installed relay is implied.

`submit` durably enqueues, then makes one dispatch attempt. `--queue-only` only enqueues. `--account auto|acct1|acct2|acct3` selects a preference; auto ranks eligible accounts by active reservations, then fraction of weekly allowance consumed. CDP availability, quota, unresolved sibling custody and explicit account holds gate selection. The relay performs authoritative model/readiness checks immediately before typing/sending. Socket availability alone is not authentication proof.

The issue plus exact prompt content determines the job identity. Repeating it returns the same request, including when it is uncertain. Do not edit a prompt or invent another ID to retry an ambiguous send. Each request freezes its generated packet hashes. Missing custody is not silently rebuilt after a relay reservation. `assign` may move only a queued request with no JOB or ledger reservation.

## Scheduling

Use **one existing native Hermes cron job**, no additional coordinator or daemon. Its no-agent script is `ops/hermes-cron-scripts/pro_route_dispatch.py`, installed under the default profile's `~/.hermes/scripts/`. An exact-source descriptor at `ops/gpt6pro-relay/route/EXECUTION.json` selects the reviewed or explicitly authorized candidate checkout and pins the three payloads. A mismatch refuses before dispatch. The native job calls `dispatch` every minute; relay clocks still enforce ≥150 seconds per chat, ≥20 seconds between polls, ≥60 seconds between submits and page loads. No in-flight tab is reloaded.

The shared `route/dispatcher.lock` serializes CLI and cron, and the existing heavy/account/IP locks remain authoritative. Thinking chats do not retain the heavy lock. Up to **3 active chats per account, 6 total per IP** are permitted; this is not permission for nine at once. Verified terminal-retained evidence tabs have separately counted capacity rather than consuming one of the three live-chat slots. Unknown/unverified holds receive no extra allowance.

The queue is `ops/gpt6pro-relay/route/queue/*.json`; registered wave packets and relay JOB/ledger records remain the underlying authorities. `CONTROL.json` may contain `paused_accounts` mapping account aliases to durable reasons. Holds stop new submissions, not exact-bound polling/fetching.

## Completion and recovery

- `running` is **not** complete. A local response alone is **not** Drive verification.
- The native relay's final-chat-bound manifest verifier must succeed. `status` rehashes every local outbox payload and manifest plus the captured response; it returns exact paths and SHA256 values.
- A subprocess crash, failed send, stale reservation, or lost acknowledgement stays uncertain. The scheduler never resubmits it. Inspect the exact log, JOB, ledger and bound target.
- `requeue-unsent --job ... --authorize-preimage <sha256>` is a separate explicit operation, only after the maintained relay has proved and journaled a first-attempt `WRONG_MODEL` pre-insertion failure as `unsent_reconciled`. It does not apply reconciliation, accept UNKNOWN-as-unsent, or handle TAB_CAP. The exact backup/current bytes and STARTED/APPLIED journal are required. Once reserved again the relay blocks duplicates normally.
- `fetch` only polls/fetches an already bound running/done request. It never submits. An absent or unverifiable outbox is held for owner review; no synthetic manifest or locally manufactured Pro output is accepted.
- Legacy packet records without frozen packet hashes can still be polled/fetched; they cannot be newly submitted by the hardened route.

## Observed deployment blockers (2026-10-07)

The parent-authorized old account-2 terminal disposition succeeded and preserved its draft. However, ChatGPT restores that 13,323-character draft into every new normal chat. Temporary mode was empty; switching back restored the draft while the original remained unchanged. Account 2 is explicitly held. Do not clear its draft, reload its retained target or resend the old job. An isolated authenticated normal-chat context or a separately accepted draft-preservation recovery is still needed.

The current ChatGPT picker abbreviates the default GPT-6 family as `Pro`. The candidate recognizes only the captured effort-only/default-family schema with a unique selected GPT-6 family, maximum badge and exact 4/4 scale. The unchanged closed incoming-parent `gpt-6-pro`/null-effort witness remains mandatory before typing and at send. This compatibility change requires focused independent review.

Runtime results and remaining proof gaps are in `reports/ops/pro-route-20261007/RESULT.json`; offline tests are not three-account or three-concurrent-chat completion proof.
