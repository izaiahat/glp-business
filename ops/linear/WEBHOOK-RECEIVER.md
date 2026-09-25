> SUPERSEDED 2026-09-11 by ops/linear/WORKER-STANDARD.md — spec history, not a build order. Do not implement anything from this file that an acceptance check does not name.

# Linear webhook receiver — SPEC-ONLY; registration DISABLED

## Verified registration and reserved route
Linear webhook `0cc8e0d6-f300-4ee7-9804-80501bc3fce4` was created with `enabled:false`, `resourceTypes:[Issue, Comment]`, `allPublicTeams:false`, scoped to GLP. Parent independently read back the exact object.

A dedicated authenticated n8n draft `39b3gnAsuXXaaPwU` reserves path `ops-linear-issue-comment-l1-f0a8631d-b19f-5070-97d4-c41ee626ee56` on `https://hermesops.app.n8n.cloud`. The workflow is inactive, has no active version, and its sole node is disabled. This is an owned future route, not a deployed sink. No business runner, health or callback endpoint was reused. No registration delivery, signature test, workflow execution or activation occurred.

Exact target readback: `reports/ops/orchestration-restore-20260907/PLAN/L1/ITEM8-PARENT-WEBHOOK-READBACK.json`. The dedicated signing secret is stored privately by webhook ID under `~/.hermes/secrets/linear/webhooks/` with mode0600. Do not print it. The existing disabled registration is the only one to reconcile and later enable; do not create a duplicate.

## Receiver contract
1. Dedicated HTTPS POST only; bounded body size/time; reject unsupported content encoding rather than verifying reconstructed bytes. Preserve the exact raw bytes in memory before parsing. Read a dedicated signing secret privately; no query-string secret, payload logs or credential output.
2. Validate `Linear-Signature` as exactly 64 hexadecimal characters, compute HMAC-SHA256 over raw bytes, length-check and constant-time compare. Only then parse JSON. Malformed signature/body fails closed.
3. Require numeric finite integer `webhookTimestamp` in milliseconds and absolute clock difference <=60 seconds. Header `Linear-Timestamp` is not a substitute for signed body time. Reject missing, stale and excessive future timestamps. Require configured organization/webhook IDs and Issue/Comment allowlist; resolve/verify GLP team scope before effects. Header event must agree with body. Distinguish `create/update/remove` from agent-event vocabulary.
4. Validate delivery UUIDv4. Under one writer lock, enforce uniqueness on `(webhookId, Linear-Delivery)` plus body SHA-256; identical authenticated redelivery is a no-effect ACK, different body under an existing key is an integrity failure. Delivery header is not authentication. Also dedupe by authenticated body hash so a changed unsigned delivery header cannot replay effects. Timestamp validation precedes acceptance, including retries; live retry timestamp behavior remains to be tested, never relax signature/time rules to make retries pass.
5. Append minimal sanitized immutable acceptance record to `ops/linear/events.jsonl`, flush and fsync before HTTP200. Suggested fields: schema, sequence, received_at, signed_timestamp, delivery_id, webhook_id, organization_id, entity_type, entity_id, issue_id, action, payload_sha256, source=webhook, disposition=accepted. No raw body, actor email, comment body, PHI, secrets or tracking URLs. IDs enable authorized re-fetch. Persist dedupe and queue state crash-consistently; the append journal is authoritative and an index must rebuild after crash. A lock and fsync are required; bare concurrent JSONL appends are insufficient. Failed durable append yields non-200, never a false ACK. Protect directories/files and define retention with the owner before deployment; retention duration is not approved here.
6. Return HTTP200 within five seconds after durable acceptance, not after Discord/model work. Linear documents up to three retries at one minute, one hour and six hours, and possible disabling. Report degraded/disabled status; reconcile gaps by the parent's polling fallback.
7. Worker re-fetches exact entity using authorized GraphQL reads; handles removed/missing entities and out-of-order updates without replaying stale state. Polling and webhook ingestion converge on the same operation ledger and consumer; one ingestion must not create two issues or reminders.

## Real monitor integration, not a new executor
`scripts/lane_monitor_v2.sh:3-15` reads PLAN/LANE-MAP.json and RESULT files; lines19-26 auto-resume from progress markers, subject to PLAN/PAUSE and PAUSE-EXEMPT. `scripts/tui_send.sh:4-10` sends literal text to a mapped pane and checks whether input cleared. These are actual existing shell integration points, **not** an HTTP wake service or durable Linear subscriber. No current source proves a Linear event consumer.

Proposed owner change: extend the existing monitor to consume an authenticated, sanitized event-watermark indication from the durable journal. Coalesce pending events into at most one orchestrator-only wake per monitor tick; fixed bounded text names the local journal/watermark, never raw comment text. Resolve the current orchestrator pane from an explicitly approved mapping; no hardcoded historical pane. Respect pause/holds, a per-orchestrator in-flight flag and persisted wake key. On uncertain TUI send, reconcile the exact pane/receipt instead of repeatedly sending Enter. Never invoke business jobs, auto-resume workers or advance item markers because a Linear event arrived. Existing auto-resume semantics must not become a backdoor around K10 PASS-only progression.

The consumer may surface a request; only the orchestrator identity moves top-level state/priority/order and closes with receipt. One active top-level issue fleet-wide; child work remains inside it. Regression reopens the earlier issue, retains historical PASS and pauses later work. New ideas enter triage only. See REMINDERS.md and DISCORD-RELAY.md.

## Required future acceptance (not executed)
Raw-byte whitespace/Unicode fixture; bad/missing/short signature; old/future/missing timestamp; wrong tenant/type; tampered delivery ID; same-ID/different-body; concurrent duplicates; disk-full and crash-before/after-fsync; retry and missed-event polling reconciliation; removed/out-of-order entity; 200 latency; bounded wake under bursts; pause/no-self-dispatch; no secret/private text egress. Disabled registration readback is complete. Receiver deployment, an authorized sandbox callback and separately authorized enablement remain future acceptance. Current tests/live delivery/natural wake: UNKNOWN.

## Sources
- `reports/ops/orchestration-restore-20260907/briefs/plan/L1-LINEAR-SKILL-AND-WORKSPACE-SETUP.md:12`, items8(c),9.
- `reports/ops/orchestration-restore-20260907/PLAN/K10/RELAY.md:17-26,70-84`.
- Official full capture `reports/ops/orchestration-restore-20260907/PLAN/L1/docs/linear-app-developers-webhooks.md:39-62,107-145,215-244,305-363`, https://linear.app/developers/webhooks.
- `/home/ubuntu/.hermes/skills/linear-work-tracking/references/integrations.md`, Webhook receiver contract.
- Source read-only: `ops/n8n_dt.py:92`; `scripts/lane_monitor_v2.sh:3-35`; `scripts/tui_send.sh:4-10`. No workflow HTTP parameter values were exported.

## Retention (settled 2026-09-11, WP6)

Item 5's open "retention duration is not approved here" is now closed: **90 days live,
older rows archived, nothing deleted.** `scripts/linear_ledger_retention.py` runs daily
under `linear-ledger-retention.timer`, moving rows older than 90 days (never the last 2000)
into gzip members at `ops/linear/archive/transitions-YYYY-MM.jsonl.gz` under the ledger's own
flock. The live file always keeps the highest `seq`, so the dedupe index a restart rebuilds
still covers the retry ladder. `--verify` proves no `seq` was lost; `read_rows()` reads the
archive and the live ledger together. Mechanics: `ops/linear/webhook/README.md`.
