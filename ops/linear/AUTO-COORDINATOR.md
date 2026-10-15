# Hourly AUTO-COORDINATOR — GLP-358

**Source candidate only. Do not install, resume cron `13d3fff8734a`, or enable
anything as part of this PR. A distinct reviewer and the designated orchestrator
own the next gate.** The n8n article-carrier remains inactive.

## Execution contract

The registered shell entry owns the existing singleton lock, checks real TUI
idleness, and calls `scripts/auto_coordinator_runner.py`. The runner reserves one
of the existing eight host slots and calls a **deterministic no-model loop**,
not a prompt with unrestricted coordinator tools. No code sends text to a TUI,
resumes an operator session, edits scheduler state, or changes Linear
root/order/admission/ownership. Workers are fresh noninteractive one-shots.

Only these existing work inputs may introduce actions:

- `reports/ops/fable-plan-2-20261009/PLAN.json`, `issues`, numeric `order`;
- `reports/ops/fleet-inbox/GLP-*/*/INGEST.json`, produced by the independent S1
  ingester, with the existing `fleet-ingest-v1` schema and hash-bound artifacts.

Fresh Linear GETs, native leases, pressure checks and TUI census are *readback
vetoes*, never a second assignment source. Do not discover work from an inbox
message's prose, historical reports, an old dispatch ledger or a model's ideas.
Do not run ingestion from this loop or advance its cursors. Source disappearance,
changed evidence, malformed input, conflicting terminal rows, unknown ownership,
a live owner, parked/held state or unresolved dependency fails closed.
An empty or advisory-only inbox produces zero actions: PLAN alone cannot dispatch
work. Each action requires one eligible authenticated, same-owner inbox row.

### READY_TO_CLOSE

Join one non-support row to its exact PLAN issue and explicit owner. Re-run the
PLAN's allowlisted read-only `proof_cmd` and the maintained closer with
`--check-only`. Require `proof_complete: true`, a hash-bound worker RESULT and
an issue-specific PASS. Recheck the real board, TUI idle gate and source hashes
before reporting `closure_ready` / `ready_for_coordinator`. Do not create a closure
effect intent or call the closer with `--apply`; subprocesses use the worker role,
never orchestrator authority. Only the authorized coordinator may accept and close
Linear. Never mark a PR merged, a report complete, or an API exit code as whole-issue
acceptance.

Missing/incomplete proof reports no closure readiness; a READY_TO_CLOSE row is
not converted into dispatch work.

### REDISPATCH and refill

Send the exact `failure_text` back to **the same explicitly bound owner**, with
its already-owned checkout and the PLAN's next action and acceptance. Unknown
owner is not permission to pick an owner from the executor/model name. Refill
in numeric PLAN order, never eight *additional* slots: one coordinator plus at
most seven admitted native children. Every child uses the shared host admission
and pressure gates and holds its lease until natural exit. At most three writing
lanes with explicit disjoint `write_area` bindings; heavy work is held for the
separate existing heavy-resource admission. No timeout kills a live effect to
free capacity. The singleton FD survives through launched workers.

PLAN must explicitly bind `owner`, `worktree`, and an unambiguous supported
`executor`; writing tasks also need `write_area`, or `read_only: true` for a real
read-only task. These are existing PLAN fields, not a new dispatch queue. The
orchestrator must reconcile old composite executor strings and absent bindings;
this lane must not rewrite PLAN to make itself executable.

Routing currently implemented: `sol` = `openai-codex/gpt-6.1-sol`, medium,
Fast; `astra` = `openai-codex/gpt-6-astra-900k`, high, standard. The installed CLI
has no per-invocation speed switch, so a mismatching inherited tier refuses
before inference instead of changing shared config. Native worker toolsets
exclude delegation. Ultra/Cloud/API and Pro/Rook/Finch are not inferred fallbacks.
Pro/Rook/Finch support tickets stay open and their rows remain unchanged;
advisories do not confer closure authority on the consuming issue.

### Idempotency and alerts

Reuse `reports/ops/auto-coordinator` for causal INPUT/LOOP/board/call receipts and
issue effect fences. A fence is not a queue: it cannot introduce work. Fsync
intent before an effect. A timeout, crash, absent ACK, unknown readback or
ambiguous closer outcome latches that issue even if its fingerprint later
changes. Do not clear a fence or replay automatically. Only a verified dispatch
joined to its exact new hash-bound terminal worker RESULT may progress to
closure readiness or same-owner redispatch. Preserve historical receipts and queues.

Print one bounded status card (at most ten lines). Delivery is the **existing
native cron route to ops-alerts**, not a new webhook, channel, outbox or send
retry. Delivery uncertainty does not authorize repeating business effects. An
actual native delivery/message receipt is required for live acceptance.

## Capacity acceptance and remaining gates

Reuse the registered `subscription_capacity_monitor.s7_extensions` and
`capacity_reset_policy.reset_tick`: current per-account included quota/reset
GETs; trigger only account-2 at exactly 0%; redeem one eligible banked reset on
account-2/account-3, never oauth-4; exact-subject readback and durable
no-replay latch. The operator's historical account-3 count is not invented as a
current API observation. This PR strengthens identity continuity before and
after redemption. It does not call redemption, install the monitor or alter its
registration.

The included executor never opens a paid fallback. `api_allowed` encodes the
confirmed $45 reviewer / $30 batch / $100 Opus / $5 Haiku / $10 inaccessible
reserve split, refuses UNKNOWN/nonfinite balances and projected balances below
$5. **It is an admission oracle, not a paid dispatcher or complete category
ledger integration.** The customized runtime already has a separate live
Claude API $5 floor. Wiring atomic category reservation to that existing paid
lane remains unresolved source integration; do not claim the whole API
acceptance clause or enable paid calls from this PR.

## Next gated steps — not performed here

1. Distinct review of the exact PR head and green focused CI. Reconcile missing
   PLAN owner/checkout/resource bindings through the orchestrator only.
2. Resolve the existing paid-lane category-ledger integration; no new account,
   paid fallback, balance assumption or spending approval is implied.
3. Separately authorize merge and scoped source installation, preserving live
   edits and installed pins. Verify wrapper/source bytes and the existing
   ops-alerts delivery declaration at the installed exact head.
4. Only while the actual TUI is idle, authorize/resume existing cron
   `13d3fff8734a`, obtain one natural unattended run with >=1 real wave or closure
   and a real ops-alerts receipt, then a second idempotent run. Bind natural
   native execution IDs, INPUT/LOOP receipts, terminal worker output or exact Done
   readback, and delivery receipts. S1's real rows are required.
5. No live proof is claimed by offline fakes. Legacy causal-v1 verifier receipts
   are retained, not upgraded: its native-message proof does not authenticate
   this new no-model driver. Review the new execution-chain readback before
   accepting runtime closure. Only the orchestrator may close GLP-358.
