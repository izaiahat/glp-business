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
No maintained authenticated denial-witness producer/verifier or genuine approved
positive receipt was found in the assigned source/retained inputs. The parser
therefore refuses all denial outcomes before CLOUD_PROVED or old-key revocation:
`head_denial_attribution_required` when custody is absent, or
`head_denial_authenticated_witness_unavailable` for any supplied custody.
Self-declared enforcement/reviewer labels and a 64-hex digest are not authenticated
receipt bytes or an independent policy verdict. No supplied dictionary unlocks
revocation, including a forged persisted CLOUD_PROVED row.

The old HTTP403/503 success whitelist is removed, not replaced with a guessed
positive contract. Genuine denial-positive compatibility remains BLOCKED until
a genuine approved witness establishes actual raw outcomes and authentic immutable
receipt custody, designated non-author acceptance, and exact task/config/runtime/
nonce/HEAD/8443 target plus approved applied-policy evidence. Service, proxy, TLS,
timeout and no-response outcomes remain UNKNOWN, not policy attribution.
No adapter/store, new positive fixture, live-state edit or pilot replay is added.
Missing acceptance retains the old key; this source refusal is not permission for
a new task, retry, publication, key change or inferred policy/version join.

## Source repair candidate: fail before effects, not after publication

The scheduler template is disabled, matching the operator-disabled live cron.
`python3 scripts/codex_cloud_vpn_renewal.py --check-safety` is a no-effect check
(exit 1 while blocked). `--run` and `--mint` also stop before reading config,
constructing the credential client, minting, publishing or dispatching. This is
NOT a successful renewal or an implemented positive denial verifier.

A source repair replaces reclaim-capable admission with the normal protected
oracle and atomic lease writer under its existing critical section. ANY present
reservation, including old, malformed and same-seat reservations, defers;
inherited seats require the existing `require_held` and are not released here.
The protected fleet lock source itself is unchanged.

Browser code no longer reads pending secrets or inserts them via raw CDP.
The renewal transfer boundary `codex_cloud_vpn_vault.py` now refuses with
`registered_vault_exact_context_fill_unavailable` before reading a pending key,
staging/listing/filling a vault item, starting a browser or writing fill intent.
The registered vault API supports only an atomic origin check, not the expected
authenticated account/config and exact owned editor document/URL/target at the
credential-write boundary. An earlier precheck can expire during staging or
intent I/O; a later Save refusal cannot undo disclosure. No accepting adapter or
raw-CDP secret insertion is substituted. Reopening transfer requires a supported
registered exact-context contract which verifies all bindings in the same
operation immediately before the write and returns an exact-context receipt.
Until then even unchanged-context transfer refuses, records no VAULT_FILLED,
and makes no live future-renewal claim. Uncertain fill/save intents still refuse
replay; Save still requires its exact target/path receipt. Offline regressions
retain RV519's same-origin path/account drift counterexamples. The whole renewal
CLI also remains unreachable past its no-effect safety preflight.

The refusal is a missing provider capability, not a safe parser relaxation.
Tailscale's documented network-flow export logs only successful connections;
[denied attempts are not logged](https://tailscale.com/docs/features/logging/network-flow-logs).
Its ACL counters are not per-task/flow attestations. No approved provider-origin
record/schema and task/runtime/executed-version join exists in the retained
inputs. Keep the existing adversarial parser tests and refusal. Do not invent
a positive dictionary schema, accept raw403/503, or substitute a configured
policy or same-host response. Obtain the actual supported provider observation
and independent acceptance, or a separately authorized instrumentation/criteria
change, before implementing an accepting verifier and enabling the job.

Manual resume: use the same approved interpreter and --run. Do not edit ticket
terminal state, delete pending secret files, or remint on a stale editor label.
If credentials/device approval expire, use the secure browser vault/MFA path;
no reset/purchase/paid fallback. New sessions assert the exact signed-in email.
The original fixture path and exact /v1/fleet-status.json alias serve the same
deny-by-default projection on the tailnet IP only. Queries, traversal, other
paths and non-GET methods refuse. The ACL/Serve/Funnel/tunnel configuration is
unchanged. Host-local tests are not Cloud proof.
