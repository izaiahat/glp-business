# Codex Cloud VPN key renewal — supported-path limit

Current status: **manual expiry-reminder fallback**, not automatic renewal.
No key was minted, delivered, changed or revoked by this implementation.

OpenAI's current [Cloud environments documentation](https://learn.chatgpt.com/docs/environments/cloud-environments#private-networking-vpn)
documents Advanced → VPN → Save → Publish/Republish and a new task for testing.
No documented public environment VPN-secret update API was found. An internal
browser endpoint is not a supported public API. Do not automate it by extracting
cookies/tokens. The assignment requires vault-only secret handling; there is no
available secure-vault key mint/transfer broker. Tailscale OAuth can mint auth
keys, but unattended use requires securely provisioned OAuth credentials and a
supported destination for the resulting secret. Neither prerequisite is claimed
here. The authorized Tailscale Google sign-in returned “Couldn’t sign you in”;
existing account-1 browser discovery exposed no surviving Tailscale tab.

## Installed fallback

`ops/cron.d/codex-cloud-key-renewal` runs the secret-free monitor hourly at :05.
A 73-hour threshold ensures the first warning is at least 72 hours before expiry
between hourly slots. Exact steps go to #decisions; failure/renewal-unavailable
alerts go to #ops-alerts. Steps are sent once per environment/key/expiry; alerts
at most once per UTC day. Journal-before-POST and exact readback prevent blind
replays after ambiguous delivery. This job does not bypass a paid-usage hold,
modify ACLs, or revoke a working key. Static metadata remains marked with its
original evidence source; it is not represented as a live Tailscale readback.

The original account-1 key expires 2026-10-17T03:02:50Z, account-2 expires
2026-10-17T02:16:48Z. The 140-test policy is untouched. Historical T3/T4 passing
validation is not new live validation; authenticated admin access is needed to
re-run it now.

## Exact operator continuation

1. Use the authorized Tailscale console; generate one 7-day reusable, ephemeral,
   pre-approved auth key with only `tag:codex-cloud`. Do not change policy.
2. Put it directly in a private password manager/secure browser path. Never
   place key material in Discord, reports, chat, argv or logs.
3. In the matching ChatGPT account's Settings → Codex Cloud → Environments,
   edit `hermes-evidence-account1` (izaiahtilton1@gmail.com) or
   `hermes-evidence-account2` (izaiahtilton@gmail.com). Advanced → VPN → replace
   the key, Save draft, Republish. Require Environment published.
4. Inspect included allowance before any task (no reset/credits). Start a new
   task in that exact environment. Run proxy-respecting GET:
   `http://hermes-01.tail9e25df.ts.net:18909/v1/fleet-status.json`.
   Require HTTP 200 and `codex-evidence-v1`; record task/environment/new key id.
5. Only after this proof revoke the exact previous key id in Tailscale. Obtain
   live replacement id/expiry metadata, update this maintained nonsecret JSON
   through a reviewed PR/scoped install, and retain the Cloud GET receipt.
6. Validate the unchanged entire policy against all 140 tests in authenticated
   Tailscale Admin before claiming fresh green policy acceptance.

**Remaining acceptance:** securely provision supported OAuth/mint/secret delivery,
complete one real replacement + fresh Cloud GET + old-key revocation, then turn
this fallback into genuine automatic renewal. Do not mark R1 B complete from
reminder tests or a host-local GET.
