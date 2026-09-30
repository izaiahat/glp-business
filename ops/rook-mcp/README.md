# Rook message-only adapter — source, not rollout

## Bounded communication expansion (source only)

Existing `reply_to_rook_request` schema/implementation and receive-one custody stay unchanged. Exactly five new tools:

| Tool | Required JWT scope AND RBAC permission | Contract |
| --- | --- | --- |
| `read_rook_messages` | `rook.channel.read` | One verified message or history before an optional ID, at most 25 messages and a 65 KB structured response budget. Original text, bounded attachment/reaction metadata, explicit truncation. No replay/ingestion. |
| `send_rook_message` | `rook.channel.write` | Stable safe `operation_id` (16–64 chars); at most 1900 UTF-16 text units; optional verified reply target and one file. Empty text requires a file. Parsed/reply mentions and embeds suppressed. Links stay literal: never fetched. |
| `create_rook_thread` | `rook.channel.write` | Public thread from a verified fixed-parent message only; duplicate source thread refused; name at most 100 UTF-16 units; 60-minute autoarchive; stable operation ID and exact parent/source readback. No private/forum/arbitrary channels. |
| `read_rook_file` | `rook.channel.read` | Attachment ID from a REST-verified message; only its verified Discord CDN metadata URL (bounded signed query), no caller URL/path. Pinned public TLS, no redirects/proxies, exact metadata/bytes/SHA readback. |
| `set_rook_reaction` | `rook.channel.write` | Bounded Unicode emoji or custom emoji verified in the fixed guild. Only `@me` add/remove. Removal requires this tool's receipt; preexisting unrelated same-bot reactions are not adopted. No other-user/bulk moderation. |

Fixed guild `1486887661099815126`, parent `1554668115239899196`, plus REST-verified public type-11 children only. Writes reject archived/locked children. No other guild/channel, DM, admin or moderation capability. This initial envelope is **not full-server capability**. There is no existing configurable channel allowlist field; later agreed channels need separate source/authorization work. No future routing framework is implemented here.

Files: 1–1,048,576 decoded bytes; strict base64 sends; safe ASCII basenames; exact `.txt`/`text/plain`, `.json`/`application/json`, `.pdf`/`application/pdf`, `.png`/`image/png`, `.jpg` or `.jpeg`/`image/jpeg`, `.webp`/`image/webp`. UTF-8/JSON/signatures and conservative sensitive/active-content markers are checked. **Not malware scanning, full format decoding or reliable PHI/secret classification**, especially for images/compressed PDF contents. Only approved non-sensitive files may be supplied; this lane uses no old PDF. The ordinary 32 KiB inbound RPC limit remains; file-bearing `send_rook_message` alone has a 1,500,000-byte JSON envelope exception. File/history bytes are not duplicated into text result fields.

The existing pinned HTTP, bot credential, exclusive-lock/private-store and uncertainty helpers are reused. New `operations` receipts persist subject/policy-bound IDs, hashes and result metadata, not message/file bytes; no shared state is used. Writes reserve before effects, confirm by readback, replay confirmed receipts, reject conflicts and never replay ambiguous outcomes across restart (including identical payload with changed operation ID). No automatic retries, queue/scheduler/framework, duplicate bot or plugin. Independent messages are `rook.message`, never `rook.reply`, and own gateway echoes cannot become requests.

PRMD advertises only `rook.channel.read`, `rook.channel.write` and `offline_access` additions. Each new tool also declares its actual OAuth scope in `securitySchemes` (mirrored in `_meta`) and its required RBAC permission in the description and the private `rook/requiredRbacPermissions` metadata hint; server-side JWT scope+RBAC verification remains authoritative. Those discovery hints do not prove a saved product plugin adopted refreshed scopes. No live scope/RBAC/grant change, subject change or client-side token storage/refresh is implemented. Actual verified JWT `exp` and unchanged validator leeway govern access; the product owns renewal. Parent owns server/grant readiness, independent review and live adoption. No Reconnect, token issuance, automatic refresh or rollout is implied.

For authentic exact-identity bearers, missing RBAC is HTTP403 without a consent challenge (renewed consent cannot assign a role). With the required RBAC already present, missing channel scope is HTTP403 with a Bearer `insufficient_scope` challenge including `error_description`, required `scope` and `resource_metadata`. A valid identified communication-tool envelope also receives a MCP2 `resultType: complete` error result with the identical challenge in `_meta["mcp/www_authenticate"]`. Missing/invalid/expired/wrong-identity bearers stay HTTP401; metadata-only stays locked. This source behavior does not establish that ChatGPT consumes an HTTP403 result body or displays linking UI; independent live acceptance is parent-owned. No tool inventory or schema change is included.

`tests/test_rook_communication.py::TestRookCommunication` is one focused offline group covering the five tools and touched JWT/channel/thread/message/file/HTTP/pinning/multipart/custody/mention/uncertainty boundaries. Sockets and real credential reads are denied; keys/bytes are synthetic. Exact-head CI runs it explicitly. Existing reply/bootstrap/receive-one regressions check preserved behavior. Offline PASS is not live Discord/Auth0/product compatibility, renewal, independent approval or effect proof.

Private PR96. No Auth0/tunnel setup, live credential use, channel send or listener launch is authorized by this source. Parent owns independent review, private runtime provisioning, exact-target rollout and bounded parent-TUI receiver. Do not route received text into ordinary gateway operator commands.

## Contract

- MCP 2.0 **2026-07-28 only**: `server/discover`, `tools/list`, `tools/call`, `events/list`, `events/subscribe`, `events/unsubscribe`. HTTP `POST /mcp`, JSON-RPC 2.0 with per-request `_meta` protocol version/client capabilities and matching `MCP-Protocol-Version` header. Results include `resultType: complete`. No legacy initialize, stdio or SSE session state.
- Public metadata `GET /.well-known/oauth-protected-resource/mcp` points to the explicitly configured HTTPS Auth0 issuer. This adapter is **not** an OAuth authorization server or token proxy. External Auth0 discovery, client registration/consent, S256 PKCE, audience/resource mapping and scopes remain parent-owned.
- Bearer access tokens: fixed configured issuer/JWKS, RS256 via PyJWT/cryptography, signature/expiry/issued-at/audience, exact operator `sub`; `rook.requests.read` for discovery/subscriptions, `rook.replies.write` for the tool. Never use ID tokens, guessed subjects, static bearer headers or tunnel control-plane keys as user authorization. Bearers/JWKS/Discord credentials are never logged.
- `discord.rook.request.created`, normal fresh MESSAGE_CREATE only, fixed guild `1486887661099815126`, channel `1554668115239899196`. Arguments must exactly match `{"guild_id":"1486887661099815126","channel_id":"1554668115239899196"}`. Mention the Rook bot user `1554962007059472445` (`<@1554962007059472445>` or `<@!1554962007059472445>`); the MESSAGE_CREATE user `mentions` array is authoritative when present, with exact content-token fallback only when absent. All Rook mention tokens are removed and surrounding whitespace stripped; empty remaining text creates no request. `@everyone`, `@here`, role-only mentions and other-user mentions do not trigger requests. Legacy exact case-sensitive `Rook:` prefix remains accepted with its existing nonempty stripped-suffix extraction. Up to 2,000 content characters. Operator `392067741500309517` and existing Kael bot `1486880423568211968` allowed; Rook's own bot messages, all webhooks and other identities refused. No blanket bot exclusion. Authenticated stored reply IDs excluded.
- Webhooks only: public HTTPS:443, no credentials/fragments/redirects. Every DNS answer must be globally routable; actual TCP connection is pinned to a checked IP with original hostname TLS verification/SNI. No proxy environment routing. IPv4-mapped/6to4/Teredo, private/linklocal/multicast destinations refused. StandardWebhooks library signs both challenge and event bodies; challenge echo must exactly verify before state becomes active.
- Subscription key binds canonical event/arguments/callback URL/authenticated principal/config policy. Maximum four live subscriptions, TTL 1 second–1 hour capped by authenticating JWT expiry; renew with a fresh token. Rotation verifies the new secret, keeps the same ID and dual-signs for 60 seconds. Repeated subscribe with unchanged secret renews without challenge. Unsubscribe is idempotent. 410 removes the subscription; 429/5xx/network failures get at most three synchronous attempts with stable webhook/event ID, fresh signatures. Secret and text never appear in error messages.
- One `reply_to_rook_request(request_id,text)`, no destination arguments. `request_id` is `discord-<request-message-id>`. Reply text is 1–1,200 characters and serialized UTF-16 wire length must fit 1,900. Existing sender internally loads the existing Discord token and sets `allowed_mentions: {parse: []}`. POST plus exact GET readback must confirm fixed channel, same bot author, ID and content before custody is `confirmed`; reply ID/link retained. Discord output is JSON `{"kind":"rook.reply","correlation":"discord-<id>","text":"..."}`, not a Rook-prefixed request.
- Authenticated principal/config/request binding, expiry, durable pre-send reservation, exact-content duplicate success and conflicting duplicate refusal. Failed, interrupted or uncertain POST/readback never automatically sends again; operator reconciles the fixed channel. No destination override or second sender bot.
- Private owned nonsymlink 0700 state directory, 0600 SQLite/lock files, one writer process (flock), read-only receiver. Adapter state is **not** Hermes state.db. Receiver pins issuer/audience/operator subject and verifies authenticated confirmed custody, exact message ID/content SHA, fixed bot/channel/guild, correlation and freshness. Marker/ID alone never admits self-echo. Up to 2s local read-only custody wait handles gateway-before-readback race inside the existing overall deadline. Same Unix account is the local trust boundary, not protection from an account-compromised writer.

## First-login bootstrap (metadata only; NOT executed)

Before genuine Google OAuth provisions the operator's Auth0 application-user `sub`,
the parent may launch this bounded **metadata-only** path in the same adapter:

```sh
timeout --signal=TERM --kill-after=5s 315s \
  "${ROOK_VENV:?isolated adapter venv}/bin/python" -B scripts/rook_mcp_events.py \
  --metadata-only --local-tunnel-metadata \
  --issuer "${ROOK_AUTH0_ISSUER:?verified issuer with trailing slash}" \
  --audience "${ROOK_MCP_AUDIENCE:?exact registered API audience}" \
  --port "${ROOK_MCP_PORT:?parent-approved nonprivileged port}" --max-run-seconds 300
```

No subject, cursor or state directory is required or accepted. No authenticator,
JWKS, Discord credential loader/client, DB, subscription worker, ingest or send is
enabled. Public GET `/.well-known/oauth-protected-resource/mcp` serves the actual
issuer/resource metadata. Empty POST/GET `/mcp` discovery probes and every
well-formed MCP operation return `401` with `WWW-Authenticate`, even with a valid
bearer; malformed requests retain the bounded HTTP refusals. OAuth does **not**
unlock this process: the connector may remain unauthorized until the parent stops
it and restarts full mode using the **real** newly provisioned subject, fresh cursor
and private state directory. Do not fabricate a subject to finish connection.

`--local-tunnel-metadata` derives exactly
`http://127.0.0.1:$ROOK_MCP_PORT/.well-known/oauth-protected-resource/mcp` as the
challenge URL, restricted to this loopback host, selected nonprivileged port and
path. It is the tunnel's **local discovery input**, not a guessed public OpenAI
well-known URL. Issuer/resource remain HTTPS. Without this explicit option,
`--protected-resource-metadata-url` must remain an explicit HTTPS URL.

Official tunnel-client **v0.0.15** documents local discovery and tunnel-service
rewriting of `WWW-Authenticate resource_metadata` and PRMD `resource` for the same
`tunnel_id`: [architecture](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/architecture.md#oauth-protected-mcp)
and [configuration](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md#mcp-server).
Use the same local `/mcp` endpoint and exact trusted Auth0 origin below, plus
`--harpoon.allow-plaintext-http` for this trusted local HTTP target
([connectors](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/connectors.md#oauth-protected-connector-behavior)).
No tunnel or login is launched here; offline source tests do not prove public
rewriting or successful OAuth.

## Exact full-mode launch inputs (NOT executed)

Create a separately approved isolated environment using `ops/rook-mcp/requirements.txt`; tests additionally need pytest. No host/core installs. Parent must securely make the existing Discord token available to the unchanged token loader. No new bot credentials. Parent prepares a private parent directory for adapter state, and excludes ordinary gateway dispatch for this channel.

Required nonsecret inputs:

- `ROOK_AUTH0_ISSUER=https://dev-qv5n6hpb2umayqwt.us.auth0.com/` (parent verified OIDC discovery; this worker made no live call).
- `ROOK_MCP_AUDIENCE=https://api.openai.com/v1/mcp/tunnel_6abc70ee3f548191bfaabda8ee682eee`: parent verified the dedicated RS256 Auth0 API identifier. It is not an OAuth client ID. JWT `scope` and RBAC `permissions` must both include the required `rook.requests.read` / `rook.replies.write` permission for that method.
- Use `--local-tunnel-metadata` for the documented tunnel rewriting path. Alternatively, without that option, `ROOK_PROTECTED_RESOURCE_METADATA_URL` must be an **exact verified external HTTPS PRMD URL**, exposed/mapped by the tunnel. Never infer an OpenAI well-known endpoint from the audience. Local PRMD remains `GET /.well-known/oauth-protected-resource/mcp`; verify public rewriting with the parent-owned tunnel.
- `ROOK_OPERATOR_SUBJECT`: actual authorized access-token `sub`, independently verified; never guessed from client ID or Discord identity.
- `ROOK_PRIVATE_STATE_DIR`: adapter-only private directory under an approved existing parent, shared read-only with receive-one.
- `ROOK_MCP_PORT`: available explicit nonprivileged loopback port; `ROOK_AFTER_MESSAGE_ID`: verified fresh pre-request cursor. No history replay.

Foreground adapter:

```sh
"$ROOK_VENV/bin/python" -B scripts/rook_mcp_events.py \
  --issuer "$ROOK_AUTH0_ISSUER" --audience "$ROOK_MCP_AUDIENCE" \
  --local-tunnel-metadata \
  --operator-subject "$ROOK_OPERATOR_SUBJECT" --state-dir "$ROOK_PRIVATE_STATE_DIR" \
  --port "$ROOK_MCP_PORT" --after-message "$ROOK_AFTER_MESSAGE_ID" --max-run-seconds 600
```

Optional `--allowed-host` accepts an independently verified **exact Host header** if tunnel forwarding preserves a different Host. Defaults accept only `127.0.0.1:<port>` and `localhost:<port>`; Origin headers are refused. HTTP binds only 127.0.0.1. One Discord connection, reconnect disabled, bounded 16-event queue and explicit 1–3,600 second lifespan by default. Shutdown finishes the in-flight reserved effect rather than killing/retrying it; parent outer timeout is still needed for a hard wall-clock cutoff.

For the explicitly authorized overnight message-only pilot, full mode may add
`--overnight-pilot` and replace the duration above with `--max-run-seconds 57600`
(16 hours maximum; choose a shorter bounded duration when sufficient). The flag
only extends the process lifespan: metadata-only remains capped at 3,600 seconds,
with or without it. Subscription TTL remains at most one hour and capped by the
authenticating JWT's expiry; renew with a fresh authorized token as before. It
does not extend token validity, scopes, identities, channel access, reply/send
approval or receive-one deadlines, and adds no scheduler, service or reconnect.
Uncertain external effects remain non-replayable. This is a launch-input example,
not a live launch or broader authority; runtime and the outer cutoff remain
parent-owned.

Parent-owned tunnel v0.0.15 target: `http://127.0.0.1:$ROOK_MCP_PORT/mcp`. Use HTTP per-request bearer forwarding, not stdio or a static Authorization header. Parent provisions existing tunnel/runtime-key references, `--mcp.oauth-trusted-origin https://dev-qv5n6hpb2umayqwt.us.auth0.com` and `--harpoon.allow-plaintext-http`. Do not start it from this source task.

Only actual parent TUI may launch receive-one via its existing terminal completion notification route, **before** the approved request/reply:

```sh
timeout --signal=TERM --kill-after=2s 130s "$ROOK_VENV/bin/python" -B scripts/receive_one_discord_message.py \
  --guild 1486887661099815126 --channel 1554668115239899196 \
  --correlation "discord-${ROOK_REQUEST_MESSAGE_ID}" --reply-kind rook.reply \
  --after-message "$ROOK_AFTER_REPLY_MESSAGE_ID" --allowed-author 1486880423568211968 \
  --mcp-state-dir "$ROOK_PRIVATE_STATE_DIR" --mcp-issuer "$ROOK_AUTH0_ISSUER" \
  --mcp-audience "$ROOK_MCP_AUDIENCE" --mcp-subject "$ROOK_OPERATOR_SUBJECT" --max-wait-seconds 120
```

Both cursors and the outstanding request ID must be real verified values, not these placeholders. Received text remains `UNTRUSTED_EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL`.

## Deliberate gaps / release holds

- Experimental MCP Events proposal plus one MCP2 version, not universal protocol support. No cursor replay/backfill, durable delivery retry queue, missed-event recovery, SSE, poll mode, legacy MCP/stdio, or automatic reconnect. Non-null cursor refuses; null cursor/no replay returns `truncated:false`.
- Three in-process callback attempts only. Crash or queue overflow can lose delivery; persisted reservations prevent duplicate external effects but do not prove delivery. Receiver's gateway-before-custody wait is 2s, not an indefinite reconciliation mechanism.
- JWT validation is offline after JWKS retrieval; no OAuth token introspection/immediate revocation signal. Delivery authority ends at JWT expiry (renewal needed); stop/reconfigure the adapter for immediate operator access revocation. Parent must verify actual Auth0 RFC8414 metadata/DCR or preregistered-client compatibility with the tunnel/ChatGPT, API audience/resource parameter handling, requested scopes and consent. OIDC discovery alone is not that proof. No OAuth client secret handled here.
- Expired subscriptions/old rotation secrets are removed on subsequent subscription/message activity; request/reply and delivery receipts persist for idempotency. No retention scheduler or automatic receipt pruning. Parent owns retention/reconciliation and host private-state custody. SQLite secrets are private at rest, **not encrypted**.
- No live Auth0, callback, Discord, tunnel or parent-TUI proof; no merge/install/service/gateway changes. Independent review and exact-target bounded pilot remain parent-owned.

Sources: https://developers.openai.com/plugins/build/mcp-events ; https://developers.openai.com/api/docs/guides/secure-mcp-tunnels ; MCP schema 2026-07-28 and experimental-ext-triggers-events design sketch linked from the official guide.
