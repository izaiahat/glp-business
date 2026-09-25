# MCP, API hygiene and operator notifications — capability findings

## Claude Code command verification
Installed `claude mcp add --help` executed read-only, exit0. It accepts `--transport` values `stdio,sse,http`, name + commandOrUrl, and `--scope local|user|project` (default local). This is CLI syntax proof, not a connection/authentication/tool smoke test.

Exact legacy command requested by L1 item8(e), preserved without normalization:

```sh
claude mcp add --transport sse linear https://mcp.linear.app/sse
```

The installed parser advertises SSE, but this workstream did not test whether the legacy remote `/sse` endpoint still operates. Current official captured recommendation is Streamable HTTP:

```sh
claude mcp add --transport http linear-server https://mcp.linear.app/mcp
```

Official docs then instruct opening Claude Code and running `/mcp` to authenticate. Operator-browser OAuth is a future authorized action, not performed here. Do not run both commands and duplicate the connector. Preserve the requested command as legacy documentation, recommend the official HTTP command for an authorized setup with deliberate scope. No app install, add command, OAuth/login change or Claude configuration mutation occurred.

Official server exposes finding/creating/updating objects such as issues/projects/comments; exact available tool list and API parity remain UNKNOWN until authenticated discovery. Standard `/mcp` is read-write by default; `/mcp/readonly` exposes read tools only, or request only read OAuth scope. OAuth2.1 dynamic registration and direct bearer/API-key paths are documented. A connector must not bypass the fleet client's role restrictions: use orchestrator identity for any top-level state/priority changes; don't grant write MCP to unattended fleet workers unless the same boundaries are mechanically enforced. A personal-key connection is not a native Linear agent OAuth app/delegate identity.

Sources: `reports/ops/orchestration-restore-20260907/PLAN/L1/docs/linear-app-docs-mcp.md:7-26,58-64` (https://linear.app/docs/mcp); L1 live brief line12 item8(e),9; installed help persisted under evidence/claude-mcp-add-help.txt.

## API hygiene requirements
Use GraphQL variables and explicit field selections; check HTTP status, GraphQL errors, payload success and exact target readback independently. Paginate all connections including inverse relationships using pageInfo/endCursor; a short page alone is not completeness. Filter and bound reads; keep rate-limit handling centralized. Treat HTTP429 and HTTP400/GraphQL RATELIMITED as quota signals; respect server reset/retry headers, bounded jittered backoff, shared holds and deadlines. Never retry validation/permission errors as transient availability.

Bulk create requires durable UUIDv4 mapping and digest-bound single-writer intent/effect journal; do not claim Linear supports an arbitrary Idempotency-Key header. Reconcile existing exact IDs after ambiguous mutations before retrying. A batch is not an atomic transaction across issues/comments/relations. Polling and webhook consumers share dedupe; reminders are not new issue operations. Preserve operator-owned objects and reserved Duplicate state. Personal key is raw Authorization; OAuth is Bearer; never print tokens or persist raw payloads with emails/PHI/secrets/tracking links. Store role-specific keys privately and default future fleet role to restricted operations; item9 implementation belongs to the parent/owner.

Sources: `/home/ubuntu/.hermes/skills/linear-work-tracking/SKILL.md` rules4-8; `references/integrations.md` GraphQL and webhook sections. Exact rate ceilings are deliberately not hardcoded: account quotas and current headers require live verification.

## Operator apps and notification choices
Official docs support Linear desktop and mobile notifications, with browser desktop notifications on supported browsers. Settings: Account → Notifications. Enable relevant desktop/mobile channels and OS/browser permissions; verify actual delivery later on the operator's device. No installed device/app state or current user preferences were inspected or changed.

Subscribe to the active top-level issue, operator-input requests and launch/release review issues; manual issue subscription `Shift S`, unsubscribe `Cmd/Ctrl Shift S`. Creating/assignment/@mention auto-subscribes, but a thread mention subscribes to the thread rather than necessarily the issue. Review My Issues → Subscribed to avoid accidental all-fleet noise. Keep assignments/mentions and meaningful status/blocking updates; mute unrelated historical projects and high-volume routine machine comments where available. Native status categories are grouped, so do not promise completion-only granularity. View subscriptions can notify on specific state entries, but actual saved-view/plan support needs readback.

Desktop/mobile notifications are real-time; routing can prefer an active desktop/browser. Email is opt-in in this workspace: do not enable email/Asks forwarding as a substitute, and do not place raw forwarded emails in Linear. Native digest/immediate options do not implement our15m/30m persistent request policy. Apply operator22:00–09:00 Pacific quiet behavior across Discord push and device notification settings; native Linear quiet-hours support was not established. Fable reminders continue overnight. REMINDERS.md is the required durable policy, not an assertion that native mobile settings enforce it. Inbox retains up to2000 open notifications per documentation; it is not the durable request ledger.

Sources: `reports/ops/orchestration-restore-20260907/PLAN/L1/docs/linear-app-docs-notifications.md:5-73` (https://linear.app/docs/notifications); K10 RELAY.md§5; `.hermes.md` Communication. Slack/GitHub NOT APPLICABLE. App setup and notification delivery remain GUIDANCE-ONLY / NOT VERIFIED.

## Authenticated item8 observation
Ops accepted the existing Tilton Group invite to verify native intake and all seven saved views. Changelog and onboarding-email subscriptions were left off; no personal GitHub account was linked. The onboarding UI advertised an existing workspace GitHub connection, which was preserved. The exact intake address is stored privately in `ops/linear/email-intake-address.private.json`; its rendered local part includes a team prefix in addition to the API unique alias. Never infer it by appending a domain to the bare API alias. No actual forwarded email, MCP OAuth installation, device install, or push delivery is claimed. Fleet API-key provisioning and role enforcement remain item9.
