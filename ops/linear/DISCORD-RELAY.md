# Linear → Discord relay — SPEC-ONLY

## Route and authority
Ordinary status/blockers/progress/request packets route exactly to ops-alerts `1487300132054765629`. Payments/investor/money-in including TEST route only to payments `1525219463866089566`; no fallback to decisions or ops-alerts. Approval-bound review remains on the exact active decisions card `1524254375453786294` and its existing dispatcher/approval path. #agents is post-only. This relay is not a new approval dispatcher and cannot approve itself, impersonate independent Discord Fable, or equate a transport receipt with acceptance.

Flow: authenticated durable Linear event → sanitized projection → existing delivery transport → exact target readback → receipt. Neither webhook text nor Discord content grants arbitrary executable instructions. Slack/GitHub: NOT APPLICABLE for this workstream. L1 calls for a custom Discord relay; no native Discord integration was established by this audit.

## Existing source and missing glue
`scripts/post_discord_message.py` supplies real `send(channel_id, content)` (lines23-36) and `readback(channel_id,message_id)` (39-50). `send` sets `allowed_mentions.parse=[]`; CLI validates channel/content readback (68-85), but truncates content at1900 characters and has no durable event dedupe, dispatcher gate, money classifier, reply ingestion or sensitive-data sanitizer. Reuse only through the authorized dispatcher wrapper after these controls; do not expose the generic arbitrary-channel CLI as the relay interface. No functions were invoked.

`scripts/discord_verify_closeout_exact.py:35-51` demonstrates exact content and attachment-byte verification, but its attachment fetch is not a license to upload private raw artifacts. For this relay prefer compact text and local evidence locators. Existing approval dispatcher internals were not fully located/verified; integrating approval-bound packets is BLOCKED pending the exact current owner interface. Do not substitute a historical Boring dispatcher or a new bot-based approval path.

## Required projection and delivery behavior
- Allowlisted fields only: request ID, issue identifier, controlled short summary, owner role, requested action/options, current acceptance gap, approved local RESULT path, timestamps and revision. No raw email, PHI, secrets, private reasoning, webhook configuration URLs or affiliate tracking links. Treat both titles and comments as tainted. Avoid copying arbitrary URLs; reconstruct only an authorized canonical Linear link from verified identity when needed. Redaction failure quarantines the packet; no raw fallback.
- Escape user mentions, markdown control tricks and code/command injection. Disable mentions including replied-user mentions unless an exact authorized targeted notification requires them. Split oversized review packets deterministically with request/revision/part identifiers; do not silently truncate actionable text.
- Persist an outbox reservation keyed by source-event/request/revision/audience/reminder-slot before sending. Persist message/channel/content digest after POST and exact GET readback. On timeout or missing receipt, reconcile exact target and cross-author equivalent content before retrying. A private journal alone does not prove global non-delivery.
- Replies bind Discord channel + message ID + request ID + revision + verified author identity to exactly one active request. Exact active-card dispatcher authorization governs approval-bound actions. Reject stale/edited/superseded/ambiguous/bot/self-approved replies; clarify without clearing requests. ACK/seen is not resolution.
- Relay feedback loops are suppressed by own-source receipt identity; never turn each reminder/comment back into new triage issues. All new genuine ideas go to triage, not Todo; fleet-role comments/subissues/triage do not acquire orchestrator top-level state/priority authority.

## Acceptance boundary
Future tests must prove exact route/content readback, wrong-channel refusal, money-only routing, no self-approval, tainted-text rejection, restart/concurrent dedupe, ambiguous POST reconciliation and one-reply/one-request binding. No sends, production polling, dispatcher invocation, reply ingestion or live notification acceptance occurred here.

## Sources
- `../K10/RELAY.md` relative to PLAN is `../../K10/RELAY.md`: §§2,5 (lines17-26,70-84); exact source root `reports/ops/orchestration-restore-20260907/PLAN/K10/RELAY.md`.
- L1 live brief line12 items8(d),9; `.hermes.md` Communication and Safety.
- `scripts/post_discord_message.py:23-50,68-85`; `scripts/discord_verify_closeout_exact.py:35-51`.
- Loaded owners: discord-delivery; operator-notifications; linear-work-tracking. No current deployed relay is claimed.
