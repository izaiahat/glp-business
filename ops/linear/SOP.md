# Linear endpoint and authority reference

The sole execution instruction set is `ops/linear/WORKER-STANDARD.md`.
Shape and naming: `ops/linear/CONVENTIONS.md`; short owner loop: `ops/linear/ORCHESTRATOR-MANUAL.md`.
This file retains endpoint, identity and protected-authority reference material, not a second execution checklist.

## Identities

| Role | User | Credential | May |
|---|---|---|---|
| Orchestrator | `86ceaadc-3c24-4810-ac3a-985cfc561a05` (Izaiah) | `LINEAR_ROLE=orchestrator` → `~/.hermes/secrets/linear/api_key` | admission, all top-level state/priority/order, triage disposition, assignment, closure receipts |
| Fleet | `858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7` (ops@) | default → `~/.hermes/secrets/linear/fleet_api_key` | comment on the active root and its children, create a scoped Todo child, update its own child |
| Agent app | `ee821778-d77a-46b8-847a-f0c457e61671` — **GLP Fleet** (`glpfleet`) | `~/.hermes/secrets/linear/agent-app-token.json`, `actor=app` | be mentioned and delegated to; emit agent activities |

Roles are client policy, not OS isolation. No lane switches role to get past a refusal. A
role, an API response or a receipt never grants spend, publishing, legal/identity action or
third-party delivery. Independent acceptance stays with Izaiah or the Discord Fable seat.

## Scoped client endpoints

From `/home/ubuntu/business`. Replace uppercase placeholders with live identifiers.

```sh
python3 scripts/linear_client.py whoami
python3 scripts/linear_client.py get GLP-n
python3 scripts/linear_client.py comment GLP-n 'MINIMIZED_RECEIPT_TEXT'
python3 scripts/linear_client.py create GLP 'TITLE' --parent ACTIVE_ROOT_ID --state Todo \
        --project 'EXACT_PROJECT' --label lane:kael --label class:ops --priority 0
python3 scripts/linear_client.py update CHILD_ID --state 'In Progress'
python3 scripts/linear_triage.py --idea "<one line>" --lane lane:kael --class class:machine
python3 scripts/linear_next.py            # read-only snapshot
```

A lane cannot change a top-level issue, reparent, change project/labels/order, or escape to
Triage/Backlog. `--label` is additive. Scope enforcement is in `scripts/linear_client.py`:
the current active root and admitted, assigned direct children, not arbitrary descendants.
The idea endpoint is journal-only candidate intake, with no automatic issue/request/reminder.

## Orchestrator endpoints — `scripts/linear_orchestrator.py`

The orchestrator owns root/order/admission and the single closure implementation. Native authenticated
history carries metadata changes; closure retains result-receipt-before-state ordering and exact readback.

| Verb | Use |
|---|---|
| `transition GLP-n <State> /abs/RESULT.json` | authorized state change; Done routes through the single pass path |
| `pass GLP-n /abs/RESULT.json --brief b.md` | typed RESULT validation, `FLEET_RESULT_V1`, Done and readback; no force bypass |
| `triage GLP-n admitted "<reason>" --project "<name>" --type <task\|defect\|incident\|decision\|idea> [--priority N]` | admission as ONE operation: drops `triage`, sets the real project and `type:`, sets priority |
| `triage GLP-n <deferred\|rejected\|duplicate\|needs-input> "<reason>"` | the other dispositions (`accept/decline/defer` normalise) |
| `rework GLP-n <brief\|machine\|supply> "<note>"` | `**REWORK[cause]**` comment + the `rework:<cause>` label — the metric's machine-readable half |
| `assign GLP-n <operator\|lane:<name>\|none> "<why>"` | authorized assignment with native history |
| `promote GLP-n GLP-parent "<why>" [--blocks GLP-m]` | authorized admission under the active root with the actual dependency relation |
| `relabel GLP-n lane:<name> "<why>"` | authorized lane-label change with native history |
| `rule GLP-n "<ruling>"` | file-backed issue ruling; protected approval remains separately authenticated |
| `resolve-request GLP-n` | existing request-resolution endpoint with exact request/response binding |

Half-admitting (state moved, project still `Triage`, `triage` label kept) is the defect that
produced GLP-148/161/163; `triage_placement_problem` now refuses it and exits non-zero.

## GLP109 approved two-account protected runtime closure

`OPERATOR-DECISION-20261008-two-account-closure.md` permits only GLP109 to use
`protected_review.native` instead of a third Linear signer. The native object has
`coordinator_session_id` and `author`/`reviewer` objects with `session_id`, `path`,
`sha256`, `write_message_id`, `tool_result_message_id`. SessionDB's retained
sessions/messages must prove distinct completed native author/reviewer/coordinator,
native parent and `_delegate_from`, designated Astra, and successful `write_file`
calls whose exact bytes equal both reports. The reviewed report must bind the
exact candidate path, issue, description, frozen payload and author/coordinator;
the author report binds installed source PR/head/merge/hash. The runtime-owned
session history is authority, not report labels; missing history fails closed.

The non-author operator posts a factual adoption (`Independent protected acceptance
verified:`), **not an impersonated agent signature**, then a distinct coordinator
comment. Both retain all normal scope/evidence/author bindings and additionally
`Native independent review SHA256: <SHA256 of canonical native JSON>`. Coordinator
acceptance still binds the adoption comment/body hash. Add only `review` and
`protected_review` to the frozen RESULT; all runtime/source/CI/lint checks and
receipt-before-Done remain. Other issues and the three-identity path are unchanged.
Independent Astra must accept the repair before installation; parent alone closes.
This uses the existing local runtime/coordinator trust boundary, not OS isolation.

## Evidence — `scripts/linear_evidence.py`

```sh
python3 scripts/linear_evidence.py GLP-n /abs/RESULT.json [--dry-run]
```

Optional upload support when a remote reviewer actually needs files, not an automatic PASS step.
Uploads RESULT/screenshots/artifacts and links them with `FLEET_EVIDENCE_V1`.
Existing issue/file SHA receipts support reconciliation before any ambiguous-upload retry.
Bounds: ≤10 files, ≤20 MB each, nothing outside `/home/ubuntu/business`, nothing with a
secret-ish name, nothing whose bytes carry credential material.

## Receipt formats

- `FLEET_RESULT_V1` — `issue_id`, `outcome:"PASS"`, `result_path`, lowercase
  `result_sha256` of the actual bytes; authored through the role-scoped pass path, not by workers.
- `FLEET_REQUEST_V1` and authenticated `FLEET_REQUEST_RESPONSE_V1` — genuine external-input
  obligations and exact request UUID/revision, issue/root, actor and grant binding.
- `FLEET_EVIDENCE_V1` — optional remote-review file handles/hashes.
- Old transition/assignment/promotion/relabel/ruling/disposition families remain readable history,
  not required new emissions. Existing comments, operation IDs, timestamps and hashes remain evidence.

## Webhook receiver and agent app

`scripts/linear_webhook_receiver.py` runs as user unit `linear-webhook.service` on
`127.0.0.1:8794`, fronted by the existing Cloudflare tunnel at
`https://bridge.allocant.ai/linear/webhook` (team events) and `/linear/agent` (agent app
events). Every delivery is HMAC-SHA256 verified against the raw body, deduped on
`(webhook_id, delivery_id, webhookTimestamp, body_sha256)` and appended to
`ops/linear/transitions.jsonl` under a flock, fsynced before the 200. Health:
`curl -s http://127.0.0.1:8794/healthz`. Control: `scripts/linear_webhook_ctl.py status`.
Contract: `WEBHOOK-RECEIVER.md`; operations: `webhook/README.md`.

`scripts/linear_agent_session.py` remains an on-demand AgentActivity helper, not an automatic
TUI dispatcher or scheduled follower. The authenticated receiver remains a separate evidence owner.

Retention: `linear-ledger-retention.timer` runs `scripts/linear_ledger_retention.py` daily —
90 days stay live, older rows move to gzip members in `ops/linear/archive/`. Nothing is
deleted; `--verify` proves no seq was lost.

## Read-only audit endpoint

`hermes-default-linear-audit.timer` runs

```sh
python3 scripts/linear_audit.py --orchestrator-id 86ceaadc-3c24-4810-ac3a-985cfc561a05 \
        --team-id d349d8ab-67cb-4a91-a0b0-2ab1a4daeb5b --notify
```

under the fleet role. Current checks are implemented in `scripts/linear_audit.py`, including
authenticated history and Done RESULT identity/hash checks. It never mutates Linear.
Exit 0 clean and silent, 1 findings, 2 uncertainty. Notification target: Discord ops-alerts
`1487300132054765629`; existing notification journal/lock/markers retain delivery identity.

## Operator interaction (Izaiah)

- **Hub:** `Operator dashboard (active + next)` — the active issue, its sub-issues, what is
  queued next. `Fleet board` shows everything.
- **What needs you:** `Needs you (Izaiah)`. Each row carries a plain "What I need from you"
  in the latest orchestrator comment. Nothing else on the board needs you.
- **Answers:** the existing authenticated request-response path binds your answer to the
  exact issue/request revision; a state change alone is not approval or independent acceptance.
- **Priorities:** the current board and your explicit decisions inform orchestrator selection.
- **Notifications:** keep Linear notifications for assignments and @mentions, mute the rest.
  Discord ops-alerts carries the daily digest; #decisions carries review packets.
- `scripts/linear_operator_inbox.py` is the operator-answer lookup endpoint.

## Tier and approval reference

| Tier | Meaning |
|---|---|
| T0 | Read-only diagnosis/readback, apart from its result artifact |
| T1 | Reversible local edits within named scope |
| T2 | Money/payments, third-party sends, credentials/login/MFA/KYC, legal/identity, privacy/PHI, live publish/release, authority changes, STATE writes, or Sheet writes outside the queue |

Classification and exact-approval obligations are in the canonical instructions; existing scoped grants
do not enlarge a tier's authority. These definitions do not impose routine lint/critic ceremony.

- `scripts/linear_t2_approval.py <GLP-n>` checks `APPROVE GLP-n` against Izaiah's actor ID
  in the authenticated webhook ledger and records `ops/linear/approvals/<GLP-n>.json`.
  Protected approval binds the exact issue/step, action/target and current artifact/grant scope;
  text alone, a pane pointer, native status or old PASS is not an authenticated grant.
- `ops/linear/approvals/STANDING-*.json` preserves operator-authored standing grants in his
  own words. `ops/linear/approvals/STANDING-live-site-deploy.json` is the existing live-site
  release reference, not blanket permission for sends, spend, credentials, legal action,
  unpassed work or ads activation. Current scope must match; this document grants nothing new.
- Protected independent acceptance remains with Izaiah or the designated Discord Fable identity,
  separate from the implementer and substantive Fable collaboration. Required review remains
  bound to the current artifact digest. `scripts/brief_review.py` is the existing focused semantic
  review endpoint, not a universal planner/critic stage.
- Shared-state ownership: Kael/parent alone through `scripts/state_guard.py`; Sheet writes through
  the existing single-writer queue/CAS path. Current cursors, request IDs, locks and idempotency
  remain authoritative; exact-effect reconciliation belongs to those existing owners.

Superseded prose is preserved, not active policy: `reports/ops/system-simplification-20260914/migration/step-7/before/ops/linear/SOP.md`.
