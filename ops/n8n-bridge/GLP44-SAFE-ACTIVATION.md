# GLP44 old-resident bridge activation — source only

**Not installation, restart, network-policy or GLP44 adoption approval.** The
coordinator owns those decisions. This source does not change the approved
bridge bytes (`b02f156a8d247acc02c61c6dbc24c365ee6b22a9a7afc79646b30c49c897f072`)
or reopen PR81 F1/F2. Base is reviewed main
`a94fb3531c09c38333c2147ec730a9a9e26bc013`.

## Maintained entry and why the external bootstrap exists

`scripts/glp44_safe_bridge_activation.py` is a fixed-target, stdlib CLI. Its
no-argument/`plan` mode is inert: no token, host status, lock or network reads.
It uses the existing bridge JobStore, durable job/receipt contract, exact user
systemd unit and canonical GLP44 source-admission lock; it adds no server,
proxy, daemon, workflow, scheduler, credential or general activation framework.

The inspected host producers cannot supply this bootstrap:
- `glp_onboarding_bridge_scoped_cutover.py:183-209` stops remote workflow/tunnel
  ingress but not `cron_bridge_action.py`'s direct `127.0.0.1:8791/run`; its
  unrelated credential/workflow/smoke changes are deliberately not imported.
- `deploy_bridge_arg_spill.py` only samples running jobs and restarts.
- Old resident bridge has no reload/admission-close command. A new endpoint
  added to disk cannot close its old interpreter's ingress.

These host scripts were read, not executed. They are **not runtime imports** of
this CLI. No copied credential helper/import closure is needed: only Python
stdlib and the already-curated bridge (for offline tests) are used.

## Narrow bootstrap contract

1. Separate authorized installation stages **only** the approved bridge source
   at the existing fixed path. This CLI never copies files or installs a unit.
   It refuses any other SHA-256 before taking an ingress hold.
2. Bind the expected old `(PID, /proc start ticks)` to the exact loaded unit's
   Python argv, its actual loopback listener inode and its own cgroup-v2 scope.
   Extra start/stop commands, changed PID, missing evidence or another listener
   refuse. Do not infer resident memory from disk hash.
3. One atomic nftables `create` transaction adds the dedicated
   `inet glp44_bridge_activation` output chain. It rejects **unmarked TCP SYNs
   to exactly 127.0.0.1:8791**, independent of conntrack. Both cloudflared's
   loopback connections and host-local `/run` callers use this boundary. No
   established streams are reset; no tunnel/workflow is stopped or mutated.
   Existing table ownership is never stolen; there is no ruleset flush.
4. A privileged socket marked `0x474c5044` is the only exemption supplied by
   this code. Its fixed-address reader can issue only authenticated GET
   `/health` or `/job/<16-lowercase-hex-ID>`. No POST, supplied URL, redirect,
   proxy, arbitrary method or caller body exists. Tokens stay in memory/headers
   and are neither changed nor logged. **Ordinary unmarked status clients are
   temporarily unavailable too**; supported status/readback remains through
   this reader, not unrestricted HTTP availability. The CLI `status` operation
   is serialized with activation; during activation its own reader owns access.
5. Drain the unchanged resident queue naturally. Remember every observed ID
   belonging to that exact old PID/start pair, including requests accepted on
   pre-gate sockets. Require terminal, matching durable receipts, zero running
   and queued counts, no pre-gate request/backlog socket, only the main thread,
   and no other process anywhere in **this service cgroup subtree**. This closes
   the sampled-idle race without demanding whole-host idle. Slow bodies,
   keepalive sockets, orphan children or callback threads cause safe refusal,
   never reset/kill/discard. Historical jobs from other instances are untouched.
6. Acquire nonblocking **canonical source EX before any downstream child lock**
   (this CLI takes no child lock). Keep the same inode; bootstrap creation is
   0600 owned by existing runtime UID/GID 1000, not root. Recheck drain and
   approved bytes, fsync a restart-intent journal, and restart exactly the
   existing `n8n-bridge.service` once. An uncertain restart is never repeated.
7. Under EX and the retained ingress rule, require new PID/start identity,
   actual listener/unit binding, approved on-disk bytes, healthy empty new
   resident, unchanged terminal job-state/receipt hashes, and matching `/job`
   readback for retained IDs. Then remove only the exact-owned gate and release
   EX. The byte binding assumes all participating source writers honor the
   canonical lock and the authorized installation owner owns the unit lifecycle;
   it is not an attestation against another root actor.

The single-attempt, root-owned `/run/glp44-bridge-activation/activation.jsonl`
journal is created exclusively and fsynced by phase. Failures do not auto-open,
replay, signal, delete IDs, overwrite receipts, reset auth or retry restart.
A failed/uncertain attempt is intentionally not resumable by another `activate`
call. Installation owner must reconcile the journal, exact table, exact PID and
job/child state before a separately approved recovery. Even a `VERIFIED` journal
is written **before** gate deletion; uncertain deletion needs readback, not an
assumption. Do not delete the journal/table merely to clear a retry.

## Entry shapes (not permission to execute them)

- `python3 scripts/glp44_safe_bridge_activation.py plan` — inert source plan.
- Separately authorized root invocation: `... activate --apply --pid <old-PID>
  --start-ticks <old-start-ticks> --wait-seconds 3600`.
- Authorized privileged readback: `... status [--job-id <exact-ID>]`.

Production paths/port/service/approved hash are deliberately not CLI overrides.
The `--apply` flag is intent, not an approval token or grant of operator authority.

## GLP80 fixed pricing-registry successor

The original GLP338 `335c4f0c -> 32d9d635` edge remains unchanged. The separately
reviewed `PricingRegistryHost` receives only the five exact PR267 payloads from
merge `1dcafc20a9b5722bb2249a1b8911d66e0e49ea40`: registry, scheduled drift
manifest, cron action runner, pricing monitor and price-evidence writer. It pins
all live preimages and candidate postimages, including the initially absent
monitor, and retains the maintained importer generation `29464d1a`.

`pricing-registry-plan` is inert. Separately authorized invocation is
`activate-pricing-registry --apply --pid <actual-PID> --start-ticks <actual-ticks>
--wait 900`. It preserves the existing ingress/drain/proc-census gates, both
source locks, terminal receipt readback and unchanged bridge bytes. Under those
locks it performs supported five-path preview/apply/zero-diff before the single
existing-unit restart, then binds the startup registry and cached builder. No
baseline override, raw restart, scheduler change or direct Sheet write exists.
The independent one-attempt journal is `/run/glp80-price-registry-activation/`
`activation.jsonl`; the old journal is not reset, deleted or reused. The same
fixed nft table refuses any still-owned previous gate. Actual receiving-owner
exclusion of old importers and nonparticipating/pre-lock readers is mandatory;
this source does not turn an idle snapshot into continuous exclusion.

A successful adoption is not a pricing run. Execute and inspect the existing
registered `pricing_refresh` job separately; source-fetch success and validated
price coverage are separate outcomes, and no-confirmed-change staging must not
renew prices or verification clocks. Only the canonical approved publisher may
project an eligible candidate. Offline successor checks live in
`tests/test_glp80_registry_adoption.py` alongside the original activation group.

## Offline acceptance and remaining operator-only condition

Run only:

```
python3 -m pytest -q -p no:cacheprovider tests/test_glp44_safe_bridge_activation.py
```

This single group uses actual in-memory bridge POST handlers, JobStore workers,
harmless scratch subprocesses and durable `/job` recovery. It verifies both
origin labels at the external-gate fixture, retained queued IDs, exactly-once
completion, post-cutover admission, real flock exclusion, native proc/cgroup
parsing fixtures, exact nft command/readback shapes, marked-GET transport,
approved SHA binding and safe refusal. Network, signals and host subprocess.run
are denied; nft/systemd/SO_MARK/cgroup boundaries are fixture-backed. The
unrelated action deliberately avoids rerunning PR81 fence F1/F2.

**Not yet proven:** live nft transaction/normalized JSON readback and marked
socket behavior on the target kernel. No privileged call is authorized by this
source task. Before adopting the path, the authorized installation owner must
validate this exact gate/readback contract in a disposable isolated network
namespace with the target nft/kernel (including ordinary loopback rejection,
marked GET reachability, retained preexisting streams and deletion readback),
then separately authorize the host network hold and restart. If privileges,
JSON normalization or host network policy differ, refuse and return a source
repair; do not weaken the strict comparison or substitute an idle snapshot.

Standalone collector exclusion and the weekly native prelock launch boundary
remain **distinct** five-file adoption prerequisites. This bridge-only bootstrap
does not satisfy them, install those five files, perform collection or close
GLP44. Source review/CI is not independent acceptance or runtime proof.

nft JSON contract reference: `libnftables-json(5)` (CREATE, MATCH, binary
operations, payload/meta, REJECT). No packet-payload/HTTP string matching is used.
