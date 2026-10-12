# Codex VPN automatic renewal

Operator authority: Oct 11 R1 revision: 90-day reusable, ephemeral, pre-approved
keys, only tag:codex-cloud; scoped OAuth auth_keys Write (Read implied); no ACL
change. OAuth client does not expire. A client secret and pending auth keys stay
in ~/.hermes/secrets/codex-cloud, mode 0600 with descriptor checks. No value is
printed, placed in argv, screenshots, Discord, chat or Git.

Daily cron at 02:05 UTC invokes codex_cloud_vpn_renewal.py --run. It renews only
when the effective current key has <14 days remaining. OAuth tokens are minted
in-process. Each account uses a separately bound authorized browser and a fresh
harness session for the supported VPN field -> Save draft -> Publish path.
Public state is state/codex-cloud-vpn-renewal.json. Active registry updates only
after authenticated NEW Cloud proof and successful old-key revocation.

The proof uses the existing DOT1 native dispatch path, one-open-ticket guard,
exact account/environment, unique nonce, authenticated completion and actual
GET 200/codex-evidence-v1 plus denied :8443. No local CLI/Mac substitutes. A
pending proof is resumed read-only, not resent. Ambiguous mint/POST/publish/
revoke phases refuse; old keys remain until proof. Failure notifications use
the existing ops-alerts route and a durable exact-readback journal.

Manual resume: use the same approved interpreter and --run. Do not edit ticket
terminal state, delete pending secret files, or remint on a stale editor label.
If credentials/device approval expire, use the secure browser vault/MFA path;
no reset/purchase/paid fallback. New sessions assert the exact signed-in email.
The original fixture path and exact /v1/fleet-status.json alias serve the same
deny-by-default projection on the tailnet IP only. Queries, traversal, other
paths and non-GET methods refuse. The ACL/Serve/Funnel/tunnel configuration is
unchanged. Host-local tests are not Cloud proof.
