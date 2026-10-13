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

Raw HEAD HTTP403/503 is transport evidence, **not** a policy-denial verdict.
The parser refuses `head_denial_attribution_required` before CLOUD_PROVED or
old-key revocation unless the coordinator supplies separate `head_denial_custody`
from the designated independent Fable review of the exact attributable denial.
That nonsecret custody binds task_id, runtime_environment_id,
environment_config_id, nonce, method HEAD, the exact target
`https://hermes-01.tail9e25df.ts.net:8443/`, port, command_exit and http, plus
enforcement `tailscale_acl`, accepted_by `designated independent Fable reviewer`
and the accepted evidence's receipt_sha256. Proxy/origin errors, missing/stale
bindings and native-reply attribution assertions refuse. The coordinator must
authenticate the actual independent verdict and exact receipt bytes upstream;
the parser checks binding, not reviewer identity or network policy itself.
No automatic custody producer or permission to edit live state is introduced.
Missing acceptance retains the old key; it is not permission for a new task,
retry, publication, key change or inferred policy/version join.

Manual resume: use the same approved interpreter and --run. Do not edit ticket
terminal state, delete pending secret files, or remint on a stale editor label.
If credentials/device approval expire, use the secure browser vault/MFA path;
no reset/purchase/paid fallback. New sessions assert the exact signed-in email.
The original fixture path and exact /v1/fleet-status.json alias serve the same
deny-by-default projection on the tailnet IP only. Queries, traversal, other
paths and non-GET methods refuse. The ACL/Serve/Funnel/tunnel configuration is
unchanged. Host-local tests are not Cloud proof.
