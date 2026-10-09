# Rook guild-scoped adapter — source, not rollout

## First Research dot (source only; minimum reply/read pilot)

Rook remains the default (`--profile rook`, or omit the flag): existing IDs, Kael observation,
Rook sending, tool inventory, OAuth/RBAC names, legacy `Rook:` trigger and reply receipts remain unchanged.
Research is opt-in, fixed to the same Kael HQ guild, with a **distinct configured channel and bot**.
Unknown Research IDs remain owner-supplied inputs; nothing here provisions or launches a service.

Research advertises and dispatches **only** `reply_to_research_request` and `read_research_messages`.
Required scope **and** RBAC permissions are `research.replies.write` and `research.channel.read`,
respectively; discovery/subscriptions require `research.requests.read`. Metadata additionally lists
`offline_access`. No generic API, GitHub comment, uncorrelated send, file download/upload, threads or
reactions are enabled for this first pilot. `discord_api` and `github_pr_comment` are refused on the
server even if the bearer has Rook or Research write scopes. Additional communication tools require
a separately reviewed source change, not a launcher flag. No admin/GitHub/Linear/host-filesystem tool.

Both observation and REST sending use Research's own token reference. Request senders must be exactly
Izaiah `392067741500309517`, Kael `1486880423568211968`, and Rook `1554962007059472445`, provided as three
`--allowed-request-sender` flags (no extra/duplicate identities). Only literal `<@BOT_ID>` / `<@!BOT_ID>`
in fresh exact-channel messages triggers Research; reply-ping metadata alone does not. Self, webhooks,
other senders, empty mention-only bodies, acknowledgments starting `ack`/`acknowledged`/`received` or
`Research handoff`/`Research reply`, and the legacy `Rook:` prefix do not create assignments.
Any admitted external text, including Rook's, is untrusted data and never approval.

Research needs a separate private state directory. A durable identity binding pins guild/channel,
bot/observer, token **path** (not its value), exact sender set, two-tool profile and OAuth policy;
repointing any field or reusing Rook state is refused. Existing Rook receipt policy hashes stay
unchanged; a small profile marker prevents a Rook directory being adopted by Research. Do not copy
state between dots. The existing lock, stable `discord-<message-id>` IDs, pre-effect reservation,
exact-author/content readback and no resend after restart uncertainty remain authoritative.
The footer is `research.reply · discord-<id>`; the receiver independently pins the same identity and
OAuth policy and reads confirmed private custody. Same Unix owner is not compromise-resistant isolation.

### Owner-only future configuration (not executed or authorized by this PR)

Select and log into the intended ChatGPT account; verify connector eligibility/quota and OAuth consent.
Separately authorize and provision a distinct Research bot/channel, connector/tunnel/API audience,
verified OAuth subject, narrow scopes/RBAC, private state, port/cursor and supervised runtime paths.
Discord permissions: View Channel, Read Message History and Send Messages on its channel only; enable
Message Content intent for observation. Never Administrator. Verify host/Auth0/tunnel/Discord limits
and costs before reuse. Do not use Rook's DB, tunnel credential, service or mutable launcher paths.
No host launcher/services, Auth0/Discord config or existing token helper are changed here.

After that separate authorization, append these identity flags to the existing bounded adapter CLI
alongside its required issuer/audience/subject/metadata/state/port/fresh-cursor/lifespan arguments:

```sh
--profile research --research-channel "$RESEARCH_CHANNEL_ID" --research-bot "$RESEARCH_BOT_ID" \
  --research-token-path "$RESEARCH_TOKEN_PATH" \
  --allowed-request-sender 392067741500309517 \
  --allowed-request-sender 1486880423568211968 \
  --allowed-request-sender 1554962007059472445
```

The token path must reference an owner-stored private token file; never supply a token value in CLI,
chat, PR or logs. The separately verified owner helper remains untouched. Receiver: use the **same**
identity flags plus `--guild 1486887661099815126 --channel "$RESEARCH_CHANNEL_ID"`,
`--allowed-author "$RESEARCH_BOT_ID" --reply-kind research.reply`, fresh correlation/cursor/bounded wait,
and all four existing `--mcp-state-dir`, `--mcp-issuer`, `--mcp-audience`, `--mcp-subject` custody inputs.
It observes through Research's bot and admits only the exact authenticated confirmed Research reply.
The parent retains the result artifact and owns separate review and authorized board updates.

Offline acceptance: `tests/test_rook_communication.py::TestResearchCommunication`; existing exact-head
CI already runs this file, plus the Rook/receiver regressions. Synthetic credentials/transport only;
passing tests do not prove live login, provisioning, OAuth compatibility or the round trip.

## Finch receiving confinement proposal (source only, adoption held)

The observed receiving adapter ran as UID/GID 1000 with `NoNewPrivs: 0`, no
seccomp filter, an unconfined AppArmor label, and access to a user account with
passwordless sudo. Non-root ownership alone therefore did not establish the
PR237 requirement that the adapter cannot publish privileged review records.
The follow-up source adds a proposed receiving boundary without changing
authentication, identity, token ownership or the existing private SQLite.

Maintained proposal files:

- `ops/rook-mcp/finch_adapter_launch.py`, based on the exact 5,871-byte host
  launcher SHA256 `ec64deecf03affc6c86d59184ea24c72687a74f6c098e3d07c9d4db9d6642a72`
- `ops/rook-mcp/finch-mcp-receiving-v1.apparmor`, a complete named profile with no
  rule includes and one explicit root-owned, hash-bound AppArmor 4.0 ABI leaf
- `ops/rook-mcp/finch-mcp-adapter.system.service`, a proposed system-managed
  service using the existing UID/GID 1000, preserving restart, resource and
  infinite graceful-stop settings; this requires a separately approved migration
- `tests/test_finch_receiving_sandbox.py`, synthetic-only source/launcher gates;
  these do not load a policy or prove actual kernel denials

Nothing in this repository installs a profile, creates a system unit or root
contract, changes permission, executes sudo, starts/stops a service, or retries
a held artifact. Parent-owned independent source review and explicit runtime
authorization remain necessary. The receiving host must prove the exact loaded
policy and effective service settings; source assertions and profile names
alone are not that proof.

### Boundary and preserved behavior

The system service manager must apply the enforcing named AppArmor profile **before the first
Python instruction**, with NoNewPrivileges and a native-ABI seccomp filter.
The launcher uses `-I -S -B`, skips all site initialization, refuses unexpected
`LD_*`/`PYTHON*`/GLIBC loader environment, checks kernel UID/GID, capabilities,
NNP, seccomp and enforcing profile before Git/import/config/network work,
then replaces its environment with four explicit nonsecret values. Neither
`PYTHONPATH`, cwd imports, `.pth`, `sitecustomize` nor a user-selected interpreter
can become a pre-confinement bootstrap. Git gets a separate clean environment,
global/system configuration disabled, hooks/fsmonitor disabled and optional
writes disabled. Git and Python executions inherit the same profile; no
unconfined profile transition is granted.

The observed stdin was `/dev/null`; stdout/stderr shared a stream whose
reciprocal peer was `/run/systemd/journal/stdout`. The launcher requires that
exact stdio shape, including root peer credentials, and closes/verifies every
nonstdio descriptor **at new process startup**, before token or Discord work.
It does not close descriptors in a running adapter. Listener, SQLite, event
poll, gateway and self-pipe sockets are subsequently created inside confinement.
Only anonymous same-profile UNIX stream peers and the exact journal path are
allowed; there is no blanket UNIX, DBus, ssh-agent or user-manager allowance.

Filesystem writes are limited to the existing adapter writer lock, SQLite and
its three explicit sidecars. Code, venv, launcher, setup readbacks, token,
receiving contract, AppArmor source and reviewed-file records are read-only.
The profile denies capabilities, DBus, mounting and outgoing peer tracing;
signals are sent only within the profile. Existing supplementary groups do
not grant paths or operations absent from the MAC policy. Policy/source loading,
unit management, scheduling and arbitrary executable paths are not allowed.
The system manager empties both capability bounding and ambient sets before
dropping to the unchanged numeric UID/GID; both launcher and review gate verify
all five capability sets are empty.
The seccomp proposal also removes unused SysV/POSIX-message-queue and keyring
interfaces plus peer process-memory/pidfd controls from `@system-service`;
`pipe`/`pipe2` remain available for the existing Git subprocesses.

The exact original bot, guild, two channels, sender set, token reference,
issuer/audience and operator validation remain unchanged. The same state path
is passed to the same adapter, with no clearing, conversion or ownership change.
Full starts still read one current message per channel and require fresh
canonical cursors. Existing delivered pending requests and durable confirmed or
uncertain receipts remain in place; fresh polling is not state deletion or
permission to replay. Metadata-only and check-only retain their separate gates.
The launcher invokes the validated adapter in the same interpreter to avoid a
second Python startup. Existing bounded lifetime and graceful draining remain.

### Separate root-owned receiving contract

The required `/etc/finch-mcp/receiving.json` is **receiving configuration, not a
whole-file review grant**. It cannot authorize an upload, independent review,
operator approval or held-report release. A separately authorized privileged
publisher creates it only after accepting the exact source and receiving plan.
Every ancestor and the regular single-link file must be root-owned and not
group/other-writable; symlinks, replacement during reading and duplicate/extra
fields fail closed. It contains exactly:

- `schema: finch-receiving-contract-v1`
- `source_commit`: exact accepted 40-character commit SHA, never a branch name
- SHA256 fields `source_sha256`, `receiver_sha256`, `poster_sha256`,
  `launcher_sha256`, `profile_sha256`, `profile_abi_sha256`, `network_guard_sha256`,
  `network_facts_sha256`, pinning the adapter,
  two local imports, installed launcher, complete AppArmor source and its exact
  `/etc/apparmor.d/abi/4.0` feature-ABI dependency, root-owned network helper and
  exact independently verified receiving network facts

The existing nonsecret setup's `source_commit`, clean checkout HEAD and all
source hashes must agree. This replaces the host launcher's literal deployed
HEAD pin without embedding a commit SHA into the commit that defines it. The
parent must separately authorize the source-pin/config update; other setup or
authentication fields must not change. No contract-generation tool is included. The separate narrowly scoped network
installer below requires already approved root provisioning. The launcher compiles already hash-verified local module
bytes so path reopening or stale bytecode cannot substitute another module.

The adapter checks kernel confinement and all contract hashes at every local
whole-file review validation stage. The complete receiving-contract digest is
also bound into the exact current-request review. Missing, unsafe, changed or
unconfined receiving state refuses the exception; PR237 hard denials and
reviewer/operator/expiry/no-replay rules remain intact. Compiled kernel policy
identity and the absence of outside privilege brokers require independent
receiver-side verification, not trust in the on-disk policy name or JSON alone.
The profile declares its 4.0 policy ABI explicitly rather than inheriting a
legacy compiler default. The ABI file must have the same root-owned nonsymlink
custody as the policy; its exact bytes require independent receiving acceptance.
CI retains its strict native-kernel rule-enforcement warning gate. A diagnostic
reference-ABI compile is labeled separately and cannot replace that gate or
claim that the receiving kernel enforces the policy.

### Finch-only network boundary and reproducible source invocations

`finch_network.py` renders one `inet finch_mcp_egress` OUTPUT table. The only
selector is the current **service cgroup**, never UID1000 or all host traffic.
Systemd v255 manages the `cgroupsv2` set across starts; because it ignores NFTSet
failures, a privileged admission condition independently reads back the entire
managed table and requires exactly the current cgroup ID before either launcher
condition or application starts. Duplicate/stale IDs, policy edits, a namespace
or boot change, stale facts, missing tables and ignored NFTSet failures refuse
startup. The adapter rechecks root custody, helper/facts hashes, current cgroup,
boot, namespaces and the admission expiry at every reviewed-file stage. It does
not receive NET_ADMIN, nft access, a service-manager connection or a root helper
execution rule. Root administrators remain trusted; changing the live rules
behind a running admitted process is prohibited outside the approved stop/drain
flow. There is no automatic reinstall or fallback to an unfiltered process.

Rule order is deliberately narrow:

1. Drop invalid traffic. Permit TCP replies only with conntrack direction REPLY,
   ORIGINAL destination `127.0.0.1:18806`, source `127.0.0.1:18806` and an exact
   independently verified loopback tunnel peer. Source port alone is insufficient
2. Deny both metadata addresses `169.254.169.254` and `fd00:ec2::254`, including
   before any resolver exception. Allow ORIGINAL new/established DNS only to
   verified exact IPs, port53 and the recorded UDP/TCP transports
3. Deny non-unicast/local routes (including a public IP routed to this host),
   all inventoried host/control addresses, IPv4 private/loopback/link-local,
   tailnet/CGNAT and reserved ranges; IPv6 must be global `2000::/3` with special
   ranges and inventoried addresses excluded. Mapped IPv4 uses its IPv4 route;
   no generic loopback, tailnet, established/related or port443 bypass exists
4. Permit remaining ORIGINAL new/established TCP443. Reject everything else in
   the Finch chain. Other service cgroups keep their existing host policy

There is no claim that port443 alone authenticates TLS. Existing application
TLS verification/URL pinning remains required. DNS replies and incoming MCP
traffic are not blocked by this OUTPUT-only table; only Finch's outgoing side
is selected. This is not a replacement for the AppArmor IPC/exec/ptrace boundary.

**Required receiving facts must not be guessed.** Parent readback on2026-10-06
verified systemd255.4, nftables1.0.9, configured resolver127.0.0.53 with TCP/UDP53
listeners, and a no-effect `nft --check` acceptance of the cgroupsv2 OUTPUT
selector. This is not set-population or enforcement proof, nor a compile of
this entire generated policy. The proxy is configured127.0.0.1:18807 to
127.0.0.1:18806; the actual live tunnel peer remains unobserved. No complete
root facts record exists here. A
separately authorized root publisher must create `/etc/finch-mcp/network-facts.json`
with exactly the closed `validate_facts` schema: `schema=finch-network-facts-v1`,
`verified_by`, `evidence`, numeric `verified_at`/`valid_until` (at most24h), exact
`boot_id`, true `unified_cgroup_v2`, `nft_socket_cgroupv2`, `nft_inet`,
`conntrack_direction`, `legacy_user_unit_disabled`, exact positive `namespace_ids`
for `net` and `cgroup`, exact `nft_version`, and explicit lists `dns`,
`ingress_peers`, `host_addresses`, `control_networks`. Each DNS entry is exactly
`address`, `port=53`, `transports` (UDP/TCP subset). Tunnel peers must be exact
IPv4 loopback addresses because the existing listener is127.0.0.1. Inventory
must include all IPv4/IPv6 local, public, Tailscale and other control addresses;
an empty control-prefix list is only valid if the independent inventory actually
found none. Provenance strings are root-publisher attestations, not identity
verification performed by code. No fact or approval record is supplied by this PR.

The receiving contract additionally pins `network_guard_sha256` to the exact
installed helper and `network_facts_sha256` to the exact facts bytes. These are
separate receiving prerequisites, not file-review grants. Facts renewal, helper
updates and boot changes require the approved stopped-service install path and
new contract/hash acceptance; a grant bound to an old contract cannot silently
follow these changes. Cached successful or uncertain operation receipts retain
the existing reconciliation/no-resend behavior even after facts/grants expire.

**Offline denial/compatibility invocation, safe in the checkout:**

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/test_finch_network.py tests/test_finch_receiving_sandbox.py \
  tests/test_finch_file_review.py tests/test_rook_communication.py \
  tests/test_rook_mcp_events.py tests/test_receive_one_discord_message.py
```

These are modeled synthetic positive/denial tests. They make no network,
credential, nft, service, profile or receiving-host calls. They are NOT actual
kernel-denial evidence. Numeric `nft --check` is also only syntax/admission,
not evidence that forbidden traffic was blocked. The parent must independently
verify actual socket-cgroup coverage for IPv4/IPv6, DNS/TLS/MCP compatibility,
control/metadata denial, before-first-network ordering and restart membership.
Unsupported features/readback formats are blockers, not a reason to loosen rules.

**Installation invocation, NOT run by this source task:** this is a privileged
security-setting operation requiring the existing separately approved runtime
flow. First drain and disable the legacy user service and stop the system
service, preserving its state/lock/sidecars/pending requests/receipts. Independently
verify native `/usr/bin/{env,sha256sum,python3,systemctl}`, `/usr/sbin/nft`, dynamic
loader, Python standard library and their root-controlled import/library closure.
Stage the accepted helper at `/usr/local/libexec/finch-mcp/finch_network.py` with
root-owned nonwritable ancestry and a single-link root-owned0644 leaf. Never run
an application-owned checkout, virtualenv, `.pth`, sitecustomize, Git helper or
writable config as root. Publish the accepted facts/receiving contract under the
same root custody. Provision `/run/finch-mcp` root0755 and its `network.lock`
root0600 at every boot before install. Publish a root-owned0644
`/etc/finch-mcp/network-helper.sha256` containing exactly the independently
accepted helper SHA256, two spaces, and the fixed installed helper path. Verify
this manifest's root custody and exact content BEFORE enabling the unit. This
native checksum condition precedes any privileged Python instruction; isolated
system Python uses `-I -S -B` and a cleared environment. `+` privilege prefixes
are limited to those two fixed condition commands, never the Finch launcher.
The unit and all drop-ins must be root-owned and independently accepted.

After that approved provisioning, the parent may use the exact commands:

```sh
/usr/bin/sha256sum --check --status /etc/finch-mcp/network-helper.sha256
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B \
  /usr/local/libexec/finch-mcp/finch_network.py install \
  --approval-reference 'EXACT-EXISTING-APPROVED-RUNTIME-FLOW-REFERENCE'
```

The reference is an audit input, not an approval mechanism; replace it only with
actual prior authorization. The helper refuses absent/stale facts, wrong boot or
namespace, untrusted custody, an active system service, missing legacy exclusion,
unowned table collisions, old-policy drift or live membership. It checks then
atomically installs/replaces ONLY its own table, and records exact kernel
readback. It never flushes the host ruleset, changes credentials/permissions,
loads AppArmor, enables/starts/stops services or edits adapter state. A failure
is not automatically retried: inspect actual table/receipt state before any
separately authorized recovery. Normal parent-owned profile/unit installation,
verified effective settings and the approved single-owner migration remain
separate; starting the unit runs the read-only kernel admission guard and writes
only its root-owned admission receipt before Finch's confined code. No network
installation is automatically performed by service startup. After a verified
boot change, the explicit install command can recreate an absent table only
with fresh current-boot facts and a new approval reference (the previous boot
reference is refused). The privileged operator must independently verify the
current approval itself; a different string alone grants no authority. Missing
tables on the same boot remain unexplained drift and are refused.

References: [systemd v255 NFTSet lifecycle](https://github.com/systemd/systemd/blob/v255/man/systemd.resource-control.xml),
[v255 directive parser](https://github.com/systemd/systemd/blob/v255/src/core/load-fragment-gperf.gperf.in)
(the service directive belongs in `[Service]`, despite the documentation example's
`[Unit]`), [privileged executable prefixes](https://github.com/systemd/systemd/blob/v255/man/systemd.service.xml),
[nft socket/conntrack/FIB expressions](https://netfilter.org/projects/nftables/manpage.html),
[kernel socket-cgroup evaluation](https://github.com/torvalds/linux/blob/v6.12/net/netfilter/nft_socket.c).

### Opt-in target-kernel harness (source only; separate runtime approval)

`finch_kernel_harness.py` is a bounded executable fixture, not a service, grant,
installer, background monitor or ordinary CI job. Its two phases make different
claims. Neither has been executed by this source task. For the accepted d04eed63
baseline, the parent has reported full generated-policy `nft --check` and strict
receiving AppArmor compilation; neither is packet/startup/restart evidence.
A successful source test
or a printed plan is not target-kernel evidence. Its flags and approval-reference
string record opt-in intent; they do not supply the actual required approval.

The ordinary offline invocation now includes:

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_finch_kernel_harness.py
/usr/bin/python3 -I -S -B ops/rook-mcp/finch_kernel_harness.py plan
```

These tests mock every vendor/process/network effect. They exercise orchestration,
evidence requirements, namespace/worker binding, skip semantics, source pins,
preimage/ownership checks and refusal/cleanup branches. CI must NEVER invoke
`packet-fixture`, `admission-fixture` or `_worker`.

Before either real phase, the parent needs **new exact approval for that phase's
privileged fixture effects**, exclusive control of the affected resources and
verified root-owned native/import closure. Stage the accepted source at
`/usr/local/libexec/finch-mcp/finch_kernel_harness.py` through the approved root
publication path, without running code from an app-owned checkout. Verify the
root custody of that file, the unchanged accepted `finch_network.py`, all
ancestors, system Python/stdlib/loader and the native `ip`, `nft`, `openssl`,
`systemctl`, `journalctl`, `stat`, `env` and `sha256sum` binaries. Publish the
independently accepted helper/harness hashes and fixed paths in the root-owned
`/etc/finch-mcp/kernel-harness.sha256`. No such manifest is supplied by this PR.
Use an isolated environment with no injected loader/Python variables:

```sh
/usr/bin/sha256sum --check --status /etc/finch-mcp/kernel-harness.sha256
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B \
  /usr/local/libexec/finch-mcp/finch_kernel_harness.py packet-fixture \
  --approved-fixture --approval-reference 'EXACT-APPROVED-PACKET-FIXTURE-REFERENCE'
```

The packet phase creates only two uniquely named disposable network namespaces,
a veth between them, a uniquely named fixture cgroup with a level-two `app`
child, and private temporary TLS files. It installs the exact accepted renderer's
OUTPUT policy **inside the client namespace**, with fixture-only membership;
it does not alter the production host table or claim real service admission.
Neither namespace has a default/external route or physical interface. Namespace
and cgroup creation identities are recorded and checked before use/removal.
Workers receive a one-shot inherited root FIFO context bound to the actual
controller PID/start identity, explicit approved packet-phase command, exact
namespace and cgroup, and source hash before reading a job or opening a socket.
Standalone/unisolated `_worker` invocation is refused.

Nineteen cases cover actual TCP and unconnected UDP DNS53, self-signed fixture
TLS443 over IPv4/IPv6, the selected listener's incoming127.0.0.1:18806 replies,
and denied loopback/private/tailnet/link-local/metadata addresses, IPv4-mapped
forms and outbound source-port18806 bypass attempts. Every sink is synthetic and
owned by the fixture. Each case first proves unrelated-cgroup sink reachability.
A denial requires the expected OUTPUT rule counter to increase AND zero new
packets in the sink's INPUT counter, in addition to a failed socket exchange.
Positive cases require rule and sink counts plus a successful exchange. The
throwaway certificate/key authenticates only `finch.invalid` inside the isolated
fixture; it is generated there and deleted on cleanup. This is not production
Auth0/Discord TLS, DNS/NSS, tunnel or application compatibility proof.

The real admission phase is a different, explicitly consequential fixture:

```sh
/usr/bin/sha256sum --check --status /etc/finch-mcp/kernel-harness.sha256
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B \
  /usr/local/libexec/finch-mcp/finch_kernel_harness.py admission-fixture \
  --approved-fixture --approved-stopped-production-unit \
  --approval-reference 'EXACT-APPROVED-STOPPED-SYSTEM-UNIT-FIXTURE-REFERENCE'
```

It **refuses unless the exact already provisioned SYSTEM unit is inactive**, its
old process/cgroup/membership is gone, and no drop-ins exist. The parent must
first gracefully drain and disable the legacy user service under the existing
approved migration plan; the harness does not stop an active Finch or perform
that migration. Real root receiving facts, installed-policy receipt, source
pins, host boot/namespaces and original guard checks must already be valid.
A differently named transient unit cannot satisfy the guard's hard-pinned real
cgroup, so there is no test bypass or relaxed guard for an isolated namespace.

Under that separately approved stopped-unit fixture, it creates one temporary
runtime drop-in, retains the original native checksum and real admission
conditions, and replaces only downstream launcher/application commands with a
credential-free marker. The existing UID/GID, AppArmor, NNP/seccomp and resource
settings remain inherited. An extra native observer records the actual cgroup
ID before the guard. Bounded fault conditions test absent table, empty/stale/
extra membership and changed fingerprint. All must show the **real guard's
exit78**, no later application/packet marker, unchanged admission receipt, zero
counted application packets where the table exists, and zero packets at the
harness-owned denied sink. ExecCondition78 can mean a skipped inactive unit with
`Result=success`; it is not reported as necessarily `failed`.

Two positive starts require fresh admission nonces and equality with each start's
actual SYSTEM cgroup ID/set membership. ID changes are not assumed. The marker
attempts one datagram to a harness-owned host-loopback UDP socket on an exclusively
bound ephemeral port, never an existing listener or external address. The deny
rule must count it while the owned sink receives zero packets. After each stop,
the actual service cgroup and its membership must be removed. Journal evidence
is restricted to the exact current invocation ID. The real Finch launcher, its
auth/token loading, SQLite/pending requests and report handling never run in
this fixture. Thus this proves the exact retained pre-application admission
chain, not the substituted launcher check or actual production startup.

The controller has a180-second work budget and a separate30-second cleanup
budget; namespace workers alarm after160 seconds and each SYSTEM marker alarms
after120 seconds, even if the controller disappears. It captures the validated baseline policy
before effects, so facts expiry does not block restoration of that stopped-service
baseline. Before each start it rechecks base-unit bytes, singleton owned drop-in
and exact effective executable/argument composition. Before restoration it
recognizes only its exact planned table mutations and owned drop-in/resource
identities. Unknown administrator changes, foreign/uncertain control processes
or restoration uncertainty cause a manual-recovery hold, not a blind overwrite,
retry or production restart. Partial uncertain files are not treated as safely
owned complete artifacts. Successful cleanup restores the accepted table and
original unit configuration, leaves the service stopped, and removes only this
invocation's resources. The last nonsecret test admission is left bound to its
removed cgroup; a future approved start must obtain a new admission.

**All other root network/unit writers must be quiescent for the approved test
window.** Preimage checks detect observed changes, but native CLI readback plus
apply is not a caller-pinned atomic compare-and-swap against an arbitrary
concurrent root administrator. This remains an explicit receiving prerequisite.
A root-only singleton `/run/finch-kernel-harness.lock` prevents overlapping copies;
on any failed fixture it remains as a recovery hold. The parent must inspect
actual processes, namespaced resources, table and unit state before any approved
cleanup or retry. Do not blindly remove the lock, replay the harness, start real
Finch, or infer authorization from a PASS report.

References: [iproute2 network namespaces](https://man7.org/linux/man-pages/man8/ip-netns.8.html),
[Linux cgroupv2 process membership](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html),
[systemd v255 service/ExecCondition semantics](https://github.com/systemd/systemd/blob/v255/man/systemd.service.xml),
[exact invocation journal filtering](https://www.freedesktop.org/software/systemd/man/255/journalctl.html).

The ordered host-only acceptance and rollback commands are in
[FINCH_RECEIVING_RUNBOOK.md](FINCH_RECEIVING_RUNBOOK.md). That document is a
source-bound plan, not an execution or deployment record.

### Mandatory receiving acceptance still outstanding

1. Compile this exact AppArmor source with the receiving parser without loading
   it, confirm supported mediation/features and inspect the compiled policy;
   no missing-feature/complain fallback. Independently approve its eventual load
2. Verify the effective system-service settings apply the named profile,
   NNP and seccomp **before interpreter startup**. Prove expected UID/GID,
   zero bounding/effective/permitted/inheritable/ambient capabilities, enforce mode and
   descriptor/environment custody. Do not infer this from a unit text file. The systemd v255 MAC documentation describes these options
   as system-manager-only; an earlier user-unit drop-in design was withdrawn.
   Do not install this as a user unit or fall back to an unenforced profile
3. Run a separately authorized bounded denial harness inside the same receiving
   profile: sudo/setuid helpers, user/system DBus and transient-unit activation,
   external UNIX/abstract peers, SSH agent/container brokers, peer ptrace/proc
   memory/process-vm/signal/FD routes, writable-code poisoning, profile escape,
   root-record mutation and inherited-descriptor substitution must be denied
4. AppArmor INET rules here are coarse. They preserve DNS, public HTTPS/gateway
   and loopback HTTP, but **do not prove exclusion of privileged TCP/UDP brokers**.
   The proposed service-cgroup nft layer above supplies this source mechanism;
   the parent must independently verify its actual exclusion of inventoried
   local/SSH/control/metadata routes and its compatibility on the receiving host.
   Any unresolved route blocks adoption; no source-only or current socket
   observation waives this prerequisite
5. Verify actual DNS/NSS/TLS, journal peer, dedicated venv imports, Git checks,
   loopback listener, fresh cursors and the receiver's required read-only
   custody access. A missing dependency requires review, not broadening the
   policy ad hoc. Confirm no unconfined competing state/source writer or
   rollback route undermines durable single-writer receipts
6. After separate approval, publish the exact root contract/profile/launcher
   and source-pin change through the parent-owned path. Gracefully drain the
   legacy `systemctl --user` service before disabling its restart/activation
   path; do not kill a reserved in-flight effect or reset any receipt. Confirm
   the old process has exited and cannot restart before starting the SYSTEM
   service with UID/GID 1000. The unchanged SQLite writer lock is a backstop,
   not permission for competing services. Verify unchanged state identity,
   pending requests, no-replay receipts and fresh startup cursors. Keep a
   reviewed rollback plan; rollback must not introduce a second writer or
   revive uncertain effects. Only then consider an independently reviewed file grant

The cloud workspace lacks `apparmor_parser`; its source tests do not claim a
successful profile compilation or receiving kernel enforcement. Systemd and
AppArmor behavior must be checked against the receiving versions. References:
[systemd v255 execution policy](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml),
[AppArmor UNIX/peer mediation](https://www.apparmor.net/man/4.0/apparmor.d/),
[Linux No New Privileges](https://docs.kernel.org/userspace-api/no_new_privs.html).

## Finch local whole-file review exception (source only)

Medical terminology can occur in a nonsensitive market report. For Finch
`text/plain` only, the adapter can accept an independent **whole-file** review
of the exact current-request bytes instead of rejecting medical terminology
alone. This is not a classifier, a proximity heuristic or a standing hash
allowlist. The reviewer must inspect the entire decoded UTF-8 file, including
distant identifiers, mixed sections and encoded/obfuscated material; any
personal or sensitive content, uncertain section or unreviewed encoding means
no nonsensitive verdict. A false attestation by the trusted publisher/reviewer
is outside what a local file permission check can prove.

Hard secret, credential, DOB/SSN and active-content markers always refuse, even
with an otherwise valid review. Existing format, size, filename, UTF-8 and
non-Finch restrictions stay enforced. No client-supplied review/grant fields,
paths, hashes, destinations, new tool or environment-variable bypass exist.
Only an already delivered, owned, ready Finch request can use this exception.

### Trust boundary and adoption prerequisites

The sole lookup is `/etc/finch-file-reviews/<request_id>.json`, derived from the
authenticated request ID. The adapter never creates this directory or writes
a record. A separately authorized privileged operator must verify the real
independent review and the operator's exact-file approval, then publish their
attestation through the parent-owned administrative process. Merely supplying
JSON in chat, an upload, the state directory or an app-owned file is not an
approval. This PR supplies no grant-issuing CLI, real record, permission change
or runtime provisioning step.

The adapter must run non-root, without elevated filesystem capabilities or
another path to privileged writes. Root execution is explicitly refused for
the exception. Every ancestor directory and record must be root-owned and not
group/other-writable, and must be accessible read-only to the adapter. Symlinks,
nonregular files and multiply linked records are refused. The receiving host
must independently verify these administrative boundaries before adoption.
This does not defend against a compromised root, dishonest privileged
attestation publisher or arbitrary code executing inside the adapter. Existing
single-writer adapter-state custody remains the replay trust boundary; this
change does not claim resistance to hostile rollback of its private SQLite.

All directory/file descriptors remain pinned during the attempt. Every stage
checks inode/ownership/mode, record size/timestamps, path identity and exact
record bytes; replacement, mutation, deletion or loss of custody fails closed.
The same immutable review context flows through upload validation, immediate
pre-POST validation, multipart construction, both REST attachment metadata/CDN
readbacks and final attachment custody. Root-owned mode bits are not treated as
immutability: pinning and repeated exact-byte checks are still required.

### Exact attestation contract

The closed `finch-whole-file-review-v1` JSON object contains:

- `binding`, exactly the output of the internal `Application.finch_review_binding`:
  normalized MIME, exact filename, byte length and SHA256; request ID and message
  ID; request creation/expiry; authenticated and stored subject; exact guild,
  channel and bot; stored/current/request policy hashes; executing source SHA256;
  and exact reply-text/footer SHA256
- Nonempty `file_author`, independent `reviewer` and `review_evidence`; reviewer
  must differ from author, authenticated sender, Finch bot and operator approver
- `verdict: nonsensitive`; `whole_file`, `exact_bytes`, `no_personal_data`,
  `no_secrets`, `no_active_content` and `no_unreviewed_encoding` all exactly true
- `operator_approval` with exact `operator_id`, a canonical approval `message_id`
  and `decision: approve_exact_reviewed_file`; the privileged publisher must
  verify that evidence actually approves this entire record and file
- Finite numeric `issued_at` and `expires`: issuance after request creation, no
  future issuance, unexpired at use, and expiry no later than request expiry

Unknown or duplicate fields, mismatched context and incomplete/uncertain
verdicts refuse. Records are limited to 16 KiB and contain no report bytes or
credentials. The review hashes its own complete attestation into the durable
`sending` receipt before any POST. Reusing the same file for another request
requires a fresh independent current-request attestation.

Source bootstrapping compares the executing module code object to a fresh
compilation of the source read at import, then pins the source SHA256. Stale
bytecode or source movement disables the exception; current disk bytes must
continue matching that loaded-source hash at every review check. The source
hash is calculated from actual executing source, not embedded into itself or
first trusted at grant consumption. Review and installation acceptance must
use the exact final source bytes; edits invalidate earlier records.

No effect replays after `sending`, `uncertain` or `confirmed`. A matching
authenticated receipt query can still recover the cached success/uncertainty
after record deletion or expiry, without loading a new grant or performing
network effects. Changed file/text/context conflicts. Expiry or custody loss
during an attempted send leaves an uncertain receipt for parent reconciliation,
never an automatic retry.

Offline acceptance: `tests/test_finch_file_review.py` uses only synthetic bytes
and temporary records, emulating privileged ownership for positive paths while
testing the real ownership predicates independently. The focused exact-head
workflow also runs all existing adapter, communication and receiver tests.
Passing those checks is not independent security acceptance, runtime approval,
permission provisioning, rollout authority or authority to release a held file.

## Finch dual-channel correlated artifact profile (source only)

`--profile finch` is a separate explicit profile, not a Research rename or a Rook clone.
It requires `--finch-channel`, `--dots-shared-channel`, `--finch-bot`,
`--finch-token-path` and the same three `--allowed-request-sender` IDs listed above.
Both channels must be distinct canonical numeric IDs; labels/names grant no access.
The bot is distinct, uses its own token for gateway observation and REST, and is not an admitted sender.
Rook and Research defaults, policy hashes and immutable historical receipts are unchanged.
This two-tool MCP **transport** inventory does not constrain Finch as a full execution
peer using native browser/web/delegation/files or existing GitHub/Linear connections.
Rook reports those native read connections are already available; their grants/write
capability are unverified here. Do not duplicate account setup or infer read-only
worker policy from this PR. GitHub/Linear tools, admin capabilities and any later
Linear cutover/update ownership are outside this transport change.

Full mode requires `--finch-after-message` and `--dots-shared-after-message`, one fresh
cursor for each exact binding (not `--after-message`). State pins both labeled bindings,
bot/observer, token reference, sender set, two tools/artifact capability and OAuth policy;
changing any binding requires separate empty private state. Each subscription selects
one guild/channel pair; IDs and saved rows include it. Requests reserve their original
guild/channel/message before callbacks. Durable per-channel cursors and request dedup
prevent restart replay. There is **no automatic private/shared forwarding**, crosspost or
thread route; each reply stays in its request origin. Both channel audiences/permissions
must be checked separately before any later live launch.

Only `reply_to_finch_request` and `read_finch_messages` are exposed and dispatched.
Reads require explicit allowlisted `channel_id`, retain 25-message/65KB bounds and never
create assignments. Finch reads and request-event `data` additionally retain actual Discord
user-mention metadata as `mentions: [{"id": "<canonical snowflake>"}, ...]` (IDs only,
first 100 entries), with `mentions_truncated` signaling an omitted suffix. Missing or
malformed metadata is `mentions: null` (unknown); an actual empty list stays `[]`.
Literal body tokens, role mentions and reply text never manufacture user IDs. Read
truncation also contributes to the existing message/result `truncated` flag. These
additive event fields are optional in discovery for compatibility with older events;
Rook/Research serialization and ingress/auth/custody policy are unchanged.
Finch ingress requires a literal mention from Izaiah/Kael/Rook.
When Discord's `mentions` field is present, it must contain Finch's bot ID; absent
metadata permits exact content-token fallback, but `mentions=[]` is refused in both
Finch channels. Rook's suppressed-notification explicit-tag exception is Rook-only,
not a Finch or Research policy expansion. Self, webhooks, acknowledgments, handoffs,
reply markers and metadata-only reply pings are refused. OAuth requires Finch scope **and RBAC**: `finch.requests.read`,
`finch.replies.write`, or `finch.channel.read` as applicable. Rook/Research grants alone
cannot authorize Finch tools. No arbitrary send, file-read tool, generic Discord API,
GitHub comments or GitHub/Linear read tools are added.

Correlated reply arguments are `request_id`, bounded `text` and optionally **one**
`file` with `filename`, `content_type`, `content_base64`; no destination or operation ID.
Existing upload filters, MIME/extension pairs, multipart encoding and pinned
no-redirect verified CDN bytes are reused. **Only Finch PNG** gains a 3MiB
(3,145,728-byte) decoded-file cap / 4,194,304 base64-character cap. The HTTP
JSON envelope is at most 4,227,072 bytes, and the >1,500,000-byte exception
requires `reply_to_finch_request` with `image/png` on the Finch profile. Upload,
multipart revalidation, verified CDN metadata/download and final custody use the
same scoped cap. Finch non-PNG and all non-Finch files stay 1MiB; their existing
1,500,000-byte envelope ceiling and raw marker/signature behavior remain intact.
Ordinary HTTP JSON retains 32KiB. Use a genuine safe `.txt` report; HTML, `.md`, DOCX,
ZIP and executable/sensitive bytes remain denied. Marker filters are not a complete
malware/PHI safety guarantee. Automatic mentions and embeds remain suppressed;
the existing exact literal Kael-tag allowlist is unchanged.

### Finch PNG validation restrictions (source only)

The stdlib-only streaming decoder accepts **8-bit, noninterlaced RGB or RGBA**
(color types 2/6, compression/filter methods 0). It validates every chunk length,
CRC and reserved type bit; first/unique IHDR, consecutive IDAT, empty final IEND,
no trailing bytes, a single complete zlib stream and exact decoded scanline count.
All five PNG row filters really reconstruct samples, using the preceding row.
It does not allocate a full bitmap or color-manage the image.

Bounds: 4096 per dimension; 4,194,304 pixels; at most 16,781,312 inflated scanline
bytes (including filter bytes); 1024 chunks; 64KiB compressed/output windows;
at most three 16KiB decoded-row copies plus a bounded row-assembly buffer. At most
16Mi sample reconstructions bound CPU work (no wall-clock deadline). The parser
retains at most the 3MiB input's copied IDAT payloads; transport also holds bounded
JSON/base64/multipart copies, so the decoder's sub-512KiB measured allocation is
**not** a process-wide memory cap. No dependency or live environment install.

Supported ancillary chunks are `tEXt`, `zTXt`, `iTXt`, `gAMA`, `cHRM`, `sRGB`,
`pHYs` and the narrowly framed `caBX` profile below. Fixed-size chunks must be
unique and precede IDAT. Text and caBX may occur before or after IDAT, never
between consecutive IDAT chunks. Raw ancillary payload plus expanded text has a
**shared 256KiB** budget (uncompressed text is conservatively counted twice;
caBX is counted once). zTXt/iTXt expansion uses the same bounded single-stream
inflater; text syntax/UTF-8 are checked. The unchanged sensitive/active marker
regex scans every non-IDAT chunk and actual expanded text. Only successfully
decoded IDAT image data avoids raw-byte marker scanning; invalid images and
unsupported metadata fail closed.

`caBX` accepts one uncompressed, explicitly sized JUMBF `jumb` store per PNG,
with first/unique `jumd`, root UUID `63327061-0011-0010-8000-00aa00389b71`
and label `c2pa`. The explicit full-UUID allowlist uses that suffix for containers
`c2pa`, `c2ma`, `c2um`, `c2as`; CBOR leaves `c2cl`, `c2cs`, `c2db`, `cbor`;
and JSON `json`. Embedded-file content instead uses the standard UUID
`40cb0c32-bb8a-489d-a70b-2ad6f47f4369`; the `bfdb`-prefix/suffix substitute
`62666462-0011-0010-8000-00aa00389b71` is refused as an enclosing content type.
The content/child distinction follows [C2PA 2.4 assertion stores](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html#_assertion_store)
(ISO 19566-5:2023 Annex B); the exact identifier is cross-checked against the
[pinned c2pa-rs JUMBF reference implementation](https://github.com/contentauth/c2pa-rs/blob/e4f63a233c3b5bd6f5fcd308a0d0db95ab35eae4/sdk/src/jumbf/boxes.rs)
(`JUMBF_EMBEDDED_FILE_UUID`, `JumbfEmbeddedFileBox::new`/`add_data`).
Actual boxes are `jumb`, `jumd`, `cbor`,
`json`, paired `bfdb`/`bidb`, optional final `free`, and optional description
private `c2sh` containing exactly 16 or 32 salt bytes. Description toggles must
include requestable + labeled, with only optional 4-byte ID, 32-byte signature
and private salt fields; labels are nonempty, valid UTF-8, at most 1024 bytes,
with C2PA-forbidden characters refused. Maximum depth is 8 superboxes including
the root; maximum count is 1024 boxes including descriptions, padding and salt.
Every declared box must fit its immediate parent and consume exactly its range.
Zero-to-EOF lengths, extended lengths, duplicate stores, unknown box/type UUIDs,
compressed `brob`/`c2cm`, XML/codestream/custom UUID/protected content and external
embedded references fail closed rather than being skipped.

Embedded descriptors accept `image/png`, `image/jpeg`, and the **source-only,
reject-only `image/svg+xml` subset** below, with an optional nonempty UTF-8
filename of at most 1024 bytes. Only `bidb` paired with `bfdb` MIME
`image/svg+xml` in the standard embedded-file UUID gets SVG validation.
Standalone SVG uploads remain denied; all other profiles are unchanged.
CBOR, JSON and PNG/JPEG embedded content bytes remain **opaque**: this is a
framing check, not a CBOR/JSON parser, embedded PNG/JPEG decoder, C2PA semantic
validator or provenance-trust check.
The complete raw caBX payload plus individual labels, salt and leaf payloads
are marker-scanned (FourCC boundaries must not mask standalone word markers),
including embedded image pixels; they gain **no** outer-image IDAT exemption.
Inner PNG/JPEG compressed metadata, escaped JSON and encoded CBOR values are not
expanded/interpreted: an escaped JSON or chunked CBOR semantic marker can pass
if it has no matching raw-byte marker. Even invalid JSON/CBOR or invalid embedded
PNG/JPEG content can pass supported framing. Existing word-boundary regex limits remain; marker scans
are not sanitization or a complete malware/PHI detector. No URL is fetched,
signature trusted, bytes normalized, metadata stripped or image re-encoded.

#### Embedded SVG compatibility (not general SVG safety)

The explicit namespace-aware Expat parser accepts exactly one `svg` root and
one direct, empty `path`, both in `http://www.w3.org/2000/svg`, declared once as
the root's default namespace. Whitespace-only XML text is allowed. Unnamespaced
root attributes may only be `width`, `height`, `viewBox`, `fill`; path attributes
may only be mandatory `d` and optional `fill`. Root dimensions/viewBox/fill are
optional; normal SVG defaults apply when absent. No other elements, attributes,
namespace declarations/prefixes, descendants or nonwhitespace text are accepted.

Limits before parsing: UTF-8 XML at most **8192 bytes**, no `&`, `<!` or `<?`
constructs. Numeric/predefined character references are deliberately refused,
as are XML declarations, PI, comments, CDATA, DTDs and entities. Defensive parser
handlers refuse them too; parameter-entity parsing is disabled, and no external
entity resolver is provided. Immediate start callbacks enforce two elements,
depth two and root/path attribute counts (four/two); no XML tree is built.
Non-`d` attribute values have a 128-byte UTF-8 cap. The input cap also bounds
parser allocation/work for names and attribute values before those callbacks.

The fully consumed path grammar supports only **explicit absolute `M L H V C Z`**,
with arities **2/2/1/1/6/0**, and requires initial `M`. Numeric tokens support sign,
decimal and exponent forms, comma/ASCII-whitespace separation and adjacent
signed/decimal tokens. Every coordinate/control value must be finite with
absolute value at most **1,000,000**. A path has at most **4096 UTF-8 bytes**,
**1024 operands** and **256 drawing segments**, counting `Z` but excluding moves.
Each command takes exactly one operand group: implicit repetitions, relative
commands, arcs, quadratic/reflected curves and lowercase close are refused.

Present dimensions must be positive, at most **4096**, unitless or `px` only.
Present `viewBox` must consume exactly four comma/ASCII-whitespace-separated
finite values bounded by absolute 1,000,000, with positive width/height. Fill is only `none`, `black`, `white`,
`#RGB` or `#RRGGBB` (hex digits case-insensitive). Scripts, events, foreignObject,
animation, styles/CSS, transforms, links, XML base and references are unsupported.
The narrow vocabulary intentionally sacrifices compatibility; acceptance is not
universal semantic safety, sanitization, rendering correctness or provenance
verification. Original PNG and metadata bytes, the Finch 3MiB cap, shared 256KiB
aggregate metadata cap and every existing raw marker scan remain unchanged.

The 2415-byte SVG / 2282-byte path test is newly constructed artificial geometry,
matching only reported counts (287 operands; 79 drawing segments; M8 L20 H2 V13
C36 Z8), not original artwork or proof of a live request. The actual original
bytes are unavailable; the reported request expired and is not retried.

Most fixtures are synthetic spec-shaped framing, not signed/verified provenance.
The independently published regression bytes in
`tests/fixtures/finch_cabx_embedded_file.jumb` are an unchanged 12,628-byte
embedded-file assertion extracted from the upstream
[c2pa-rs exp-test1.png](https://raw.githubusercontent.com/contentauth/c2pa-rs/e4f63a233c3b5bd6f5fcd308a0d0db95ab35eae4/sdk/tests/fixtures/exp-test1.png).
The complete upstream PNG is 5,884,439 bytes with SHA-256
`8794b09652ab0585b9337f9c85981407ee297eda20f5885964057fca59648bd3`.
Its caBX payload begins at absolute offset 41; extract caBX-relative range
`[23216,35844)` (PNG-absolute `[23257,35885)`). The resulting fixture SHA-256 is
`9fdd17837a0aa7ba7ab60c974011a3cee9ecaae2a94d868504c184942204cd61`.
It retains the published `jumd` UUID/label, `bfdb` image/jpeg descriptor, and
`bidb` bytes. Tests pin its length/hash, accept it in synthetic PNG/store wrappers
and refuse a variant changing only the enclosing UUID to the erroneous substitute.
The original PNG is **not** accepted whole: it exceeds the Finch 3MiB file limit
and its caBX exceeds the 256KiB metadata limit. Extraction is a test-only bounded
assertion fixture, not normalization of upload bytes or verification of the
original PNG's signature/provenance. The upstream MIT notice is retained beside
the fixture; optional filename/salt coverage remains explicitly synthetic.
Original avatar/banner bytes are unavailable. Finch has now reported the banner
chunk-map (IHDR, caBX, 32 consecutive IDAT, IEND), matching this chunk profile;
that report is not local verification of either original caBX payload. Both
originals still require supported uncompressed box framing, metadata bounds
and unchanged marker checks. Independent review and exact live-owner caBX scope
confirmation are required before any separately authorized runtime use.

Grayscale/indexed/palette, 16-bit, interlaced and APNG files are refused, as are
PLTE, transparency keys (`tRNS`), ICC profiles (`iCCP`), EXIF (`eXIf`), timestamps
and any unknown chunks. These compatibility restrictions are deliberate: this is
not a general PNG implementation, OCR or reliable PHI/secret/malware detection.
The original avatar/banner bytes are unavailable locally; their formats, hashes,
claimed compressed-byte marker offset and real compatibility are not verified.

`FinchIdentity.policy_fields()` and stored request/receipt hashes are unchanged;
its `correlated_file_limit` remains the generic 1MiB cap. The PNG-only source
exception is explicit above and in the tool schema, not a state migration.
Independent source review and future exact owner approval remain required;
no existing requests, runtime files, deployment or publication are changed.

The pre-effect digest binds origin, exact text/footer and normalized file metadata/bytes.
POST and GET author/context/content plus attachment ID/name/type/size/URL and downloaded
byte digest must agree before confirmation. Private receipts retain only hashes and
metadata, not bytes/base64/attachment URLs. Changed text/files conflict; uncertain or
interrupted effects never resend after restart. Receive-one uses the same Finch identity
flags plus `--channel <exact-origin>`, `--reply-kind finch.reply`, fixed author and complete
MCP custody inputs. It requires confirmed exact-origin content/file custody; it does not
download files or treat attachment metadata alone as proof of bytes.

Offline acceptance lives in `TestFinchCommunication` alongside unchanged Rook/Research
regressions, using synthetic JWTs, signed subscriptions, real HTTP/REST/multipart/CDN
code with only transport peers mocked. The existing exact-head workflow runs these
acceptance tests; no runtime workflow is enabled or broadened.
Passing tests do not prove live Discord permissions/intent, real IDs/token identity,
connector/OAuth compatibility or either live artifact round trip. The Rook-only host
launcher is unchanged and must not be repointed. No merge, provisioning, live send,
Auth0/tunnel/service/credential/board change or deployment is authorized by this source PR.

## GitHub identity (operator setup; not performed by this source change)

`github_pr_comment` posts advisory PR **issue-comments**, not reviews or approvals, through a host-held GitHub App identity. It requires the existing JWT scope **and** RBAC permission `rook.channel.write`; no new OAuth scope. Arguments are `repo` (exactly `izaiahat/glp-business` or `izaiahat/glp-site-candidate`), positive integer `pr_number`, `body` (1–20,000 characters), required 40-hex `inspected_head_sha`, and the existing safe 16–64 character `operation_id`.

Operator setup:
1. Create the App at <https://github.com/settings/apps/new>, named `rook-glp` for `rook-glp[bot]` authorship (GitHub must accept that name). Leave the webhook inactive; set repository permissions **Pull requests: Read & write**, **Contents: Read-only**, **Metadata: Read-only**. Make it installable only on this account.
2. Generate the private key yourself, and install the App on **only** `izaiahat/glp-business` and `izaiahat/glp-site-candidate`.
3. Place the PEM at `/home/ubuntu/.hermes/secrets/dot-bots/rook-github-app.pem`, owned by the adapter's Unix user; run `chmod 600` on it. The tool refuses any other mode, wrong ownership, symlink, nonregular file or invalid/non-RSA key.
4. Write the nonsecret `/home/ubuntu/.hermes/secrets/dot-bots/rook-github-app.json` with `{"app_id": <positive integer>, "installation_id": <positive integer>}` (substitute the real numeric IDs). Never paste the key into Rook/ChatGPT, a PR, logs or chat.
5. After separately approving deployment, restart `rook-mcp-adapter`. This source PR does **not** install, provision, restart or exercise the App.

The adapter validates both files before any network call, signs a host-side RS256 App JWT (`iat` 60 seconds back, expiration at most 9 minutes ahead), and exchanges it only at `POST /app/installations/{id}/access_tokens`, requesting the two repository names and only `pull_requests: write`. Installation tokens are cached **in memory only**, until five minutes before expiration. Missing/invalid configuration returns `GitHubIdentityNotConfigured` with no network calls; there is no fallback to the operator's GitHub connector/token.

Repository calls are only `GET /repos/{repo}/pulls/{n}` and `POST /repos/{repo}/issues/{n}/comments`; a plain issue or unverifiable PR head is refused. The comment begins with `**Rook advisory review** (automated; not operator approval or a formal review)`, followed by a visible `stale: inspected head differs from current head` line when heads differ and `-- inspected head: <sha> · PR head at posting: <sha> · op <operation_id>`. All generated metadata precedes the supplied Markdown, so an unclosed HTML comment or code fence in the body cannot swallow it. The current head is the pre-POST snapshot, not an atomic guard against concurrent pushes. Every `@` in the supplied body is followed by a zero-width joiner to neutralize user/team mentions. No review states, merges, pushes, closes, labels, edits or deletes are supported. The GitHub-only JSON envelope is capped at 250,000 bytes to accommodate 20,000 Unicode characters; unrelated tools retain their existing caps.

Durable reservations reuse the existing operation store: same ID and semantic arguments return only the saved comment ID/URL and repo/PR/head/status metadata with no HTTP calls. Conflicting arguments are refused. Timeouts, 5xx, malformed post responses or socket failures are uncertain and never automatically resent, even with a new operation ID or a changed current PR head. Semantic identity excludes the generated operation metadata and current-head snapshot. Receipts never store request/response bodies, JWTs, tokens, PEMs or headers; fixed sanitized errors only. Operator reconciliation is required after uncertainty. Offline transport-mocked acceptance is in `tests/test_rook_mcp_events.py::TestGitHubPRComment`; live App provisioning/authorship remains unverified and operator-owned.


## General `discord_api` tool (source only)

The six existing tools and fixed-#rook reply/ingress behavior remain unchanged. `discord_api` adds Rook's bot-Administrator latitude across Kael HQ without a new policy framework, token, bot, scope, scheduler or deployment.

- Required arguments: `operation_id` (existing safe 16–64 character pattern), `method` (`GET|POST|PATCH|PUT|DELETE`), `path` (relative Discord API v10 route, optional query, not a URL). Optional `json` object (or a channel-position array for guild channel PATCH) and `audit_reason` (1–512 characters, URL-encoded into `X-Audit-Log-Reason`). No file attachment in this tool; `send_rook_message` retains its existing upload contract.
- Authorization: the existing exact-subject/issuer/resource JWT **scope AND RBAC permission `rook.channel.write`**, including reads. The same Rook bot credential and pinned REST transport are reused, with no redirects or automatic retries.
- Scope: `/guilds/1486887661099815126` and descendants; `/channels/<id>` and descendants only after `GET /channels/<id>` verifies the guild. Thread parents are also verified recursively, including parents of payload-referenced channels. Nested body channel/guild references (followers' `webhook_channel_id`, message forwards' `message_reference.channel_id`/`guild_id`, guild channel settings, event channels, `parent_id`, channel-ID lists and position-array targets) must verify in this guild before any effect; foreign, DM, missing or unverifiable contexts fail closed. Forward references require an explicit channel. Null optional channel settings may be cleared. Exact `/users/@me` and `/applications/@me` allow GET/PATCH for the bot's own profile. Other guilds, DMs, other-user profiles, gateway, webhook-by-token, OAuth2 and interaction routes are refused. Encoded route segments are canonicalized before checks; traversal/alternate-origin paths are refused.
- Small hard-denial list: deleting/leaving the guild; kicking/banning/timing out/changing roles or nicknames of Izaiah (`392067741500309517`) or Kael (`1486880423568211968`), including bulk bans; deleting #rook (`1554668115239899196`) or ops-alerts (`1487300132054765629`) or changing their permission overwrites; editing/deleting Kael's messages, including bulk deletion. Role deletion or any role `permissions` PATCH first verifies Izaiah's and Kael's role lists and refuses held roles or unverifiable lists; @everyone permissions PATCH is refused. Guild ownership transfer and MFA-level changes are refused. Protected channel reparent+permission-sync PATCH and permission-moving/syncing position-array entries are refused; harmless position-only moves remain allowed. Leading-zero/noncanonical snowflakes are rejected before protection checks. Other ordinary guild/channel/role/thread/message/pin/reaction/emoji/event routes use Discord's own permissions and validation.
- Indirect member effects: all non-GET `/guilds/<guild>/integrations` routes are refused (`IntegrationMutationDenied`), including deletion and legacy mutation/sync routes. Discord's [Delete Guild Integration](https://docs.discord.com/developers/resources/guild#delete-guild-integration) explicitly kicks the associated bot, so an integration ID cannot be treated as an unrelated resource. Non-GET guild prune routes are also refused (`GuildPruneDenied`); [Begin Guild Prune](https://docs.discord.com/developers/resources/guild#begin-guild-prune) removes members without a per-member exclusion and the documentation provides no protected-bot guarantee. Integration reads and prune previews remain allowed. Application role-connection metadata, current-user role connections and application-command permissions already fall outside the exact `/applications/@me` / `/users/@me` roots and remain denied; no OAuth route or credential is added. Existing protected-member role deletion/permissions checks still apply.
- Parent-category overwrites: before any channel overwrite PUT/DELETE (or channel PATCH with `permission_overwrites`), fresh `GET /channels/<id>` lookups inspect **both** #rook and ops-alerts. A target matching either current `parent_id` is refused (`ProtectedChannelParentActionDenied`), regardless of whether the child currently appears synced. Discord [permission syncing](https://docs.discord.com/developers/topics/permissions#permission-syncing) propagates category overwrite edits to synced children. Failed lookups, missing parent fields, mismatched channel/guild identity or invalid parent IDs fail closed (`ProtectedChannelParentUnverifiable`); explicit null parents are valid. No parent snapshot is cached across writes. Ordinary unrelated overwrites and cosmetic category edits remain allowed. This is offline source protection, not a live parent inventory or atomic protection against concurrent external reparenting.
- Publishing/fan-out is refused: `/channels/<id>/messages/<id>/crosspost` is denied outright, even for an in-guild announcement source, because following destinations cannot be verified. Editing/deleting messages with Discord's `CROSSPOSTED` flag (including bulk deletion) is also refused: those mutations propagate to following servers. Reads and ordinary unpublished message writes remain allowed. Announcement follows and explicit message forwards still require every source/destination reference to verify in-guild. See Discord's [Crosspost Message](https://docs.discord.com/developers/resources/message#crosspost-message) and [Announcement Channel FAQ](https://support.discord.com/hc/en-us/articles/360032008192-Announcement-Channels).
- Protected-role inspection uses the real pinned transport's exact read-only member routes for Izaiah and Kael in this guild; it adds no arbitrary-member or write routes to the narrow transport. Unheld-role deletion/permission PATCH may proceed only after both verified member role lists are checked.
- Queries: route-specific documented GET parameters only (history/reaction/thread/member/ban/audit/event pagination and read flags, member search and prune preview). Unknown, duplicate and malformed parameters are refused, not forwarded. Values are validated and normalized; writes accept no query parameters. Receipts contain a canonical route plus allowlisted query metadata, never raw query strings.
- Message POSTs (also forum thread initial messages) default to `{"parse":["users"],"replied_user":false}` unless `allowed_mentions` is explicit. No default everyone/here/role ping; the five narrow communication tools retain their stricter mention behavior.
- GETs return status and Discord JSON directly, without operation receipts. Non-GETs use the existing durable operation lock/reservation/conflict/uncertainty machinery. Fresh calls return status and Discord JSON; receipt replays return **stored status/IDs/digest**, not the original body, without REST calls. Identical uncertain effects are never retried, even with a changed operation ID or `audit_reason`; effect identity is only method, normalized route/query and the outgoing body. Audit annotations are sent as headers but neither stored nor hashed into effect identity. Receipts retain only method/canonical route/allowlisted query/status/IDs/digest plus the existing subject/policy/state envelope, never payloads, avatar bytes, webhook response tokens or raw Discord error text. Webhook creation stays allowed; fresh response tokens are not saved or replayed.
- Responses are bounded to about 60k JSON characters with `truncated` explicitly set: large lists preserve a whole-item prefix; oversized objects return a JSON text prefix and original size. The transport caps input responses at 4 MiB. Discord 4xx errors are tool errors with status and Discord's message; redirects/5xx/transport failures do not get retried. A successful Discord response is not independent runtime acceptance.
- `discord_api` has a 1,500,000-byte JSON-envelope exception for base64 profile images. All other tools retain the ordinary 32 KiB envelope (except existing `send_rook_message` file uploads). Discord controls which profile fields are supported and their size limits; this does not claim bot support for arbitrary user-only profile fields.

Examples (distinct stable `operation_id` for each intended write):

```json
{"operation_id":"rook_create_room_001","method":"POST","path":"/guilds/1486887661099815126/channels","json":{"name":"rook-lab","type":0},"audit_reason":"Operator-authorized Rook setup"}
{"operation_id":"rook_avatar_update_001","method":"PATCH","path":"/users/@me","json":{"avatar":"data:image/png;base64,<PNG bytes>"}}
{"operation_id":"rook_bio_update_0001","method":"PATCH","path":"/applications/@me","json":{"description":"Rook bot bio"}}
```

Offline acceptance: `tests/test_rook_mcp_events.py::TestDiscordAPI` mocks REST/credentials/sockets and covers scope, each denial, normal Administrator routes, profile changes, durable replay/uncertainty, mentions, result truncation, audit transport, 4xx and large-image HTTP parsing. Existing Rook communication/reply/ingress suites remain the regression gates. This change does not deploy, restart, merge, change Auth0 grants/config/secrets or call live Discord.

## Bounded communication expansion (source only)

The communication expansion preserves `reply_to_rook_request` and receive-one safety/custody guards. Correlated replies now use the markdown wire format described below. Exactly five additional tools:

| Tool | Required JWT scope AND RBAC permission | Contract |
| --- | --- | --- |
| `read_rook_messages` | `rook.channel.read` | One verified message or history before an optional ID, at most 25 messages and a 65 KB structured response budget. Original text, bounded attachment/reaction metadata, explicit truncation. No replay/ingestion. |
| `send_rook_message` | `rook.channel.write` | Stable safe `operation_id` (16–64 chars); at most 1900 UTF-16 text units; optional verified reply target and one file. Empty text requires a file. Parsed/reply mentions and embeds suppressed. Links stay literal: never fetched. |
| `create_rook_thread` | `rook.channel.write` | Public thread from a verified fixed-parent message only; duplicate source thread refused; name at most 100 UTF-16 units; 60-minute autoarchive; stable operation ID and exact parent/source readback. No private/forum/arbitrary channels. |
| `read_rook_file` | `rook.channel.read` | Attachment ID from a REST-verified message; only its verified Discord CDN metadata URL (bounded signed query), no caller URL/path. Pinned public TLS, no redirects/proxies, exact metadata/bytes/SHA readback. |
| `set_rook_reaction` | `rook.channel.write` | Bounded Unicode emoji or custom emoji verified in the fixed guild. Only `@me` add/remove. Removal requires this tool's receipt; preexisting unrelated same-bot reactions are not adopted. No other-user/bulk moderation. |

The five narrow tools below stay fixed to guild `1486887661099815126`, parent `1554668115239899196`, plus REST-verified public type-11 children only. Their writes reject archived/locked children; they do not gain general admin/moderation capability. The separate `discord_api` tool above supplies the broader guild scope; fixed ingress and correlated replies still use #rook.

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
- One `reply_to_rook_request(request_id,text)`, no destination arguments. `request_id` must match `discord-[1-9][0-9]{0,19}`. Nonblank reply text is at most 1,850 characters of Discord markdown (headings, bold, bullets, code blocks); the final content including its footer must fit 2,000 UTF-16 units. The dedicated Rook sender keeps `allowed_mentions: {parse: []}` unchanged. POST plus exact GET readback must confirm fixed channel, Rook bot author, ID and content before custody is `confirmed`; reply ID/link retained. Discord content is the verbatim text followed by `\n-# rook.reply · discord-<id>`. Its full-content SHA binds text and footer. Receive-one accepts only the exact final footer with matching correlation, while continuing to accept legacy JSON `{"kind":"rook.reply","correlation":"discord-<id>","text":"..."}` under the same author/custody checks. Already-stored legacy replies retain their original digest and cached outcome; retrying identical text never resends to change the rendering.
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

Optional `--allowed-host` accepts an independently verified **exact Host header** if tunnel forwarding preserves a different Host. Defaults accept only `127.0.0.1:<port>` and `localhost:<port>`. Origin headers are refused by default. Rook may explicitly opt in with repeatable `--allowed-origin https://chatgpt.com` and/or `--allowed-origin https://chat.openai.com`; only those exact values are configurable, with no wildcard, subdomain, path or port variants. An approved but unconfigured origin remains refused, as do empty/other origins, duplicate Origin headers and wrong/duplicate Host headers. Approved origins still require the unchanged OAuth identity, scopes and RBAC permissions (no bearer remains HTTP401); this is not a CORS or authentication bypass. No live launcher, Finch configuration, OAuth grant or deployment is changed. HTTP binds only 127.0.0.1. One Discord connection, reconnect disabled, bounded 16-event queue and explicit 1–3,600 second lifespan by default. Shutdown finishes the in-flight reserved effect rather than killing/retrying it; parent outer timeout is still needed for a hard wall-clock cutoff.

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
  --after-message "$ROOK_AFTER_REPLY_MESSAGE_ID" --allowed-author 1554962007059472445 \
  --mcp-state-dir "$ROOK_PRIVATE_STATE_DIR" --mcp-issuer "$ROOK_AUTH0_ISSUER" \
  --mcp-audience "$ROOK_MCP_AUDIENCE" --mcp-subject "$ROOK_OPERATOR_SUBJECT" --max-wait-seconds 120
```

Receive-one keeps its existing Kael gateway credential loader; `--allowed-author` pins the Rook reply author, not the observer identity. MCP-bound replies require exact authenticated stored custody regardless of which bot observes them; unbound self-echo remains refused.

Both cursors and the outstanding request ID must be real verified values, not these placeholders. Received text remains `UNTRUSTED_EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL`.

## Deliberate gaps / release holds

- Experimental MCP Events proposal plus one MCP2 version, not universal protocol support. No cursor replay/backfill, durable delivery retry queue, missed-event recovery, SSE, poll mode, legacy MCP/stdio, or automatic reconnect. Non-null cursor refuses; null cursor/no replay returns `truncated:false`.
- Three in-process callback attempts only. Crash or queue overflow can lose delivery; persisted reservations prevent duplicate external effects but do not prove delivery. Receiver's gateway-before-custody wait is 2s, not an indefinite reconciliation mechanism.
- JWT validation is offline after JWKS retrieval; no OAuth token introspection/immediate revocation signal. Delivery authority ends at JWT expiry (renewal needed); stop/reconfigure the adapter for immediate operator access revocation. Parent must verify actual Auth0 RFC8414 metadata/DCR or preregistered-client compatibility with the tunnel/ChatGPT, API audience/resource parameter handling, requested scopes and consent. OIDC discovery alone is not that proof. No OAuth client secret handled here.
- Expired subscriptions/old rotation secrets are removed on subsequent subscription/message activity; request/reply and delivery receipts persist for idempotency. No retention scheduler or automatic receipt pruning. Parent owns retention/reconciliation and host private-state custody. SQLite secrets are private at rest, **not encrypted**.
- No live Auth0, callback, Discord, tunnel or parent-TUI proof; no merge/install/service/gateway changes. Independent review and exact-target bounded pilot remain parent-owned.

Sources: https://developers.openai.com/plugins/build/mcp-events ; https://developers.openai.com/api/docs/guides/secure-mcp-tunnels ; MCP schema 2026-07-28 and experimental-ext-triggers-events design sketch linked from the official guide.
