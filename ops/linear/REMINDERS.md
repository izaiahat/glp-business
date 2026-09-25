> SUPERSEDED 2026-09-11 by ops/linear/WORKER-STANDARD.md — spec history, not a build order. Do not implement anything from this file that an acceptance check does not name.

# Persistent input requests and reminders — SPEC-ONLY / NOT INSTALLED

Binding source: `reports/ops/orchestration-restore-20260907/PLAN/K10/RELAY.md` §§2,5, especially lines74-84. This supersedes per-lane next-ready dispatch. No scheduler, reminder store or delivery has been installed by this workstream.

## Request record and lifecycle
A durable request has one immutable request_id, active top-level issue ID, optional subissue ID, origin-event key, owner role, input_required_from_operator boolean, sanitized question/options, exact acceptance gap, revision, created_at, status, resolution evidence, Discord packet/message bindings, acknowledged_at and reminder-slot ledger. States are OPEN → ACKNOWLEDGED (still unresolved) → INPUT_RECEIVED → RESOLVED after justified reconciliation; ACKNOWLEDGED does not stop reminders. Supersession links old/new revisions without silently dropping obligations. Same source operation updates the same request; an unrelated request has a distinct ID.

Only orchestrator-authorized resolution changes blocker/Linear/workflow state after exact actionable input and verification. An answer resolves only its named request. Request resolution is not task PASS; task acceptance still needs actual work/readback. Fleet identity later defaults to comments, active-parent subissues and triage only. Orchestrator's operator key alone moves top-level state/priority/order and closes with receipt. One active top-level issue fleet-wide; PASS-only advancement; regressions reopen earlier issues and pause later work while retaining historical PASS.

## Cadence
- Immediate alert on request creation. It must not wait for a periodic digest. Record a durable notification intent at creation and notify Fable immediately. Post the immediate Discord request packet even overnight without an operator push/mention during quiet hours; this reconciles immediate visibility with operator quiet. This non-pinging interpretation is a candidate implementation choice, not independently tested operator-device behavior.
- Fable reminders every15minutes for every unresolved request, including overnight and ACKNOWLEDGED requests. Fable-only resolution targets Fable, not repeated operator prompts.
- Operator reminders every30minutes **only when his input is required**, excluding22:00 inclusive to09:00 exclusive in `America/Los_Angeles`. Use IANA timezone/DST conversion, not fixed UTC offsets. Never send operator reminders during quiet hours. Newly operator-required state schedules an eligible prompt without clearing original history.
- At quiet-hours end09:00, one Discord packet covers overnight requests, with separate **Resolved overnight — history, no action** and **Still actionable — exact response needed** sections. Include each request ID, owner, revision, current gap and exact reply instructions. History remains visible even when Fable fixed everything overnight. Deduplicate by local quiet-window date and packet revision; reconcile an ambiguous send before retry. The09:00 packet satisfies that operator slot; do not also send an identical30minute reminder.
- Missed ticks/restarts recompute due state from persisted UTC times plus local quiet-window boundaries. Do not burst-send every missed slot. Coalesce one current due packet per audience, while preserving a missed-slot audit. Stop operator reminders if operator input no longer required; continue Fable reminders until verified resolution.

## Integration contract
Existing `scripts/lane_monitor_v2.sh:3-35` ticks at60seconds and watches mapped results/panes; it has no persistent request engine, quiet-hours logic or Linear subscription. Proposed owner extension consumes the same durable acceptance/request journal used by polling/webhooks, evaluates due windows once per tick, persists audience/slot reservations, and uses one bounded orchestrator wake. Actual transport is `scripts/tui_send.sh:4-10`; it does not prove substantive ingestion. No separate cron/scheduler/parallel writer should be introduced without inventorying the existing monitor ownership.

Use the DISCORD-RELAY.md outbox and exact readback, not a new arbitrary-channel sender. Exact actionable replies require authenticated author, exact channel/message/request/revision and current dispatcher checks for approvals. A morning packet with multiple requests must yield separately bound actions; bare “yes”, “seen”, or an unbound emoji clears none. An actionable reply can supply input but cannot impersonate independent Fable acceptance or authorize spend/publish outside its exact gate. Reminder creation, repetition and reply retries never create duplicate issues, rerun a publisher or imply spend approval.

## Proof
One test per acceptance check; one observed natural run. (The former 17-scenario matrix was removed 2026-09-11.)

## Item8 implementation boundary
The read-only global selector and constrained triage CLI are implemented and tested. This reminder engine, its durable request store, exact reply ingestion, and natural cadence are not installed. Their absence does not authorize silent blocker skipping. Existing dispatcher ownership remains; implementation must not start a competing monitor.
