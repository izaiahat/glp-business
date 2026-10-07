# Finch receiving sequence: source-bound, host-only, approval-gated

This document is an executable **receiving plan**, not an execution record.
The source task ran no privileged fixture, service, policy, network or credential
operation. Hermes owns deployment, phase approval and closure. Do not paste all
phases into an unattended script: accept each phase's evidence before continuing.

The original inspected source was PR238 head `cb70f906bf33fbceccff42774b49b1597dbbaef1`,
stacked on PR237 `9393eab1e10b3c9d8368571373854df02fc613da` and deployed baseline
`af5cc609e0e4bd7f5c3d0164b0aeb3b95e61037e`. The integration order is
PR260 → PR237 → PR238 → PR252. The table below pins the integrated PR252
fixed-recipient adapter; guard, unit, launcher and harness pins are unchanged.
Choose the exact independently accepted integrated Git head before receiving.
A docs commit or reply-consumer installation does not approve production
confinement, service replacement, or file-reply release.

| Installed artifact | Accepted SHA256 |
| --- | --- |
| `finch_adapter_launch.py` | `8745066d434da434a482e5439f3c4e1727776a56d19402de52d0e75894dd94bb` |
| SYSTEM unit | `37fb96ae615a652778adc8e3b3e0c1c909a63ed2e2545762596e24cd6800d243` |
| AppArmor profile | `9b288c197d51bb063c1ff8821adf0647821c4499dc13dc1d07a914f62106beb8` |
| `finch_network.py` | `f006d767cb4bae30ad4fb919c4ac06104dc72fe2b61bfc894052b7f051669155` |
| `finch_kernel_harness.py` | `e474d3c484a5428b5e88bed19c76ab86c3d42257137e8746e545fd7ee5c02497` |
| Adapter `rook_mcp_events.py` | `b3c99fff316e9a2bcfe0fdbc5304d8ba7592a04150cd6c56d91d13b6debe32b8` |

## 0. Entry gate and stopped preimage

Hermes must first have separately approved and completed the staging described
in [README](README.md#separate-root-owned-receiving-contract): root-owned helper,
manifest/import closure, loaded exact enforcing profile/ABI, verified current
network facts, installed exact nft table/receipt and disabled SYSTEM unit.
The legacy USER service must already be gracefully drained and disabled, with
no other state writer or activation path. Do not stop an unknown live process to
make these prerequisites appear true. Do not copy/reset SQLite, its sidecars,
writer lock, pending requests, receipts, identities or credentials.

Require fresh facts for the actual boot, observed ingress peer, resolver and
control-address inventory. Recheck root custody and effective settings on the
receiving version. All other root unit/network writers must be quiescent for the
whole window. CLI readback/apply is not atomic CAS against another administrator.
The following commands run only in a verified root control session after the
parent approves this exact sequence and its resource changes. Approval strings
and shell variables are audit inputs, never authorization.

```bash
set -euo pipefail
umask 077
test "$(/usr/bin/id -u)" = 0
FINCH_UNIT=finch-mcp-adapter.service
FINCH_BASE=/etc/systemd/system/finch-mcp-adapter.service
FINCH_DROP_DIR=/run/systemd/system/finch-mcp-adapter.service.d
FINCH_DROP=$FINCH_DROP_DIR/90-receiving-compat.conf
FINCH_AUDIT=$(/usr/bin/mktemp -d /run/finch-acceptance.XXXXXXXX)
FINCH_PHASE_SOURCE=
FINCH_INVOCATION=

finch_base_ok() {
  test ! -L "$FINCH_BASE" || return 1
  test "$(/usr/bin/stat -c '%u:%a:%h' "$FINCH_BASE")" = 0:644:1 || return 1
  test "$(/usr/bin/sha256sum "$FINCH_BASE" | /usr/bin/cut -d ' ' -f 1)" = 37fb96ae615a652778adc8e3b3e0c1c909a63ed2e2545762596e24cd6800d243 || return 1
  test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p FragmentPath --value)" = "$FINCH_BASE" || return 1
}
finch_stopped() {
  finch_base_ok || return 1
  test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ActiveState --value)" = inactive || return 1
  test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p MainPID --value)" = 0 || return 1
  test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ControlPID --value)" = 0 || return 1
  test ! -e /sys/fs/cgroup/system.slice/finch-mcp-adapter.service || return 1
}
finch_stopped
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p UnitFileState --value)" = disabled
test -z "$(/usr/bin/systemctl show "$FINCH_UNIT" -p DropInPaths --value)"
test ! -e "$FINCH_DROP_DIR" && test ! -L "$FINCH_DROP_DIR"
test ! -e /run/finch-kernel-harness.lock
/usr/bin/sha256sum --check --status /etc/finch-mcp/network-helper.sha256
/usr/bin/sha256sum --check --status /etc/finch-mcp/kernel-harness.sha256
/usr/bin/systemctl show "$FINCH_UNIT" -p FragmentPath -p DropInPaths -p ExecCondition -p ExecStart -p User -p Group -p AppArmorProfile -p NoNewPrivileges -p CapabilityBoundingSet -p AmbientCapabilities -p SystemCallFilter -p TimeoutStopUSec > "$FINCH_AUDIT/unit.before.txt"
/usr/sbin/nft --json --numeric list table inet finch_mcp_egress > "$FINCH_AUDIT/network.before.json"
/usr/sbin/ip netns list > "$FINCH_AUDIT/netns.before.txt"
/usr/bin/cp -- "$FINCH_BASE" "$FINCH_AUDIT/base-unit.before.service"
```

Expected: the exact base file, no drop-ins, disabled/inactive unit, no live
service cgroup/member and no stale fixture lock/resources. Independently compare
the nft fingerprint with the approved installed receipt; an empty set alone is
insufficient. Preserve these nonsecret observations locally. Native manifest
contents/paths and every ancestor must already have been reviewed, not merely
accepted because a checksum command returned zero. A missing prerequisite is
**HOLD**, not permission to install, relax or regenerate anything.

## 1. Isolated namespace packet checks

After a separate packet-fixture approval, run the existing fixed harness once:

```bash
: "${FINCH_PACKET_APPROVAL:?exact already-approved packet-fixture reference}"
/usr/bin/sha256sum --check --status /etc/finch-mcp/kernel-harness.sha256
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B \
  /usr/local/libexec/finch-mcp/finch_kernel_harness.py packet-fixture \
  --approved-fixture --approval-reference "$FINCH_PACKET_APPROVAL" \
  | /usr/bin/tee "$FINCH_AUDIT/packet.json"
```

Expected: exit 0 and `result=PASS`, `phase=isolated-packet-fixture`,
`production_admission=false`, 19 cases. Each deny case needs the expected OUTPUT
counter increase **and zero new sink INPUT packets**; each positive case needs a
successful exchange and both counters. Unrelated-cgroup controls must succeed.
The certificate/key is disposable fixture material, not real service identity.

Limits: this tests the target kernel in isolated namespaces, not real service
admission, production DNS/NSS, Auth0/Discord TLS or MCP. The five AF_INET6
unconnected-UDP regressions are **fake-socket coverage only**; the existing
19-case native matrix has no dedicated IPv6 UDP case. Do not claim that missing
native acceptance from this PASS or invoke `_worker` directly to substitute it.

## 2. Real SYSTEM admission fixture

After accepting phase 1 and separately approving the stopped-unit fault fixture:

```bash
: "${FINCH_ADMISSION_APPROVAL:?exact already-approved stopped-unit fixture reference}"
finch_stopped
test -z "$(/usr/bin/systemctl show "$FINCH_UNIT" -p DropInPaths --value)"
test ! -e "$FINCH_DROP_DIR" && test ! -L "$FINCH_DROP_DIR"
/usr/bin/sha256sum --check --status /etc/finch-mcp/kernel-harness.sha256
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B \
  /usr/local/libexec/finch-mcp/finch_kernel_harness.py admission-fixture \
  --approved-fixture --approved-stopped-production-unit \
  --approval-reference "$FINCH_ADMISSION_APPROVAL" \
  | /usr/bin/tee "$FINCH_AUDIT/admission.json"
```

Expected: exit 0, `phase=real-SYSTEM-admission-fixture`, seven cases,
`actual_Finch_started=false`, `production_adoption=false`. Five faults must show
actual admission exit 78 with no later application/packet markers and unchanged
receipt. A skipped inactive unit with `Result=success` is valid refusal evidence;
`systemctl start` exit 0 alone is not success. The two positive starts require
fresh nonces and each actual cgroup ID; IDs need not differ between starts.

After success, require the original unit/table preimage, no service cgroup/member,
no drop-in and no harness lock. The marker's owned host-loopback sink is separate
from phase 1's namespaces. Neither phase runs the real launcher or proves its
production compatibility. On either fixture failure, preserve the recovery lock,
logs and resource identities. No blind rerun, `rm` of the lock, table flush,
namespace deletion or restoration over an unknown administrator change.

## 3. Bounded production-context compatibility phases

These phases are separately approved temporary SYSTEM-unit changes. Keep all
three original ExecConditions: native checksum, real network admission, and
actual launcher `--check-only`. Only ExecStart and the bounded lifecycle settings
below change. No user unit, direct shell launcher, `sudo -u`, transient differently
named unit or weakened profile substitutes for the actual service cgroup.

The helper functions below install only a reviewed, root-owned phase file from
the private audit directory. Review the exact candidate bytes before calling
`finch_phase`; the function does not approve them. No overlapping root manager is
permitted. RuntimeMaxSec bounds the active window; TimeoutStopSec remains infinity
so an in-flight reserved effect drains rather than being killed.

```bash
finch_owned_dropin() {
  finch_base_ok || return 1
  test -n "$FINCH_PHASE_SOURCE" || return 1
  test ! -L "$FINCH_DROP" || return 1
  test "$(/usr/bin/stat -c '%u:%a:%h' "$FINCH_DROP")" = 0:644:1 || return 1
  /usr/bin/cmp --silent "$FINCH_PHASE_SOURCE" "$FINCH_DROP" || return 1
  test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p DropInPaths --value)" = "$FINCH_DROP" || return 1
  test "$(/usr/bin/find "$FINCH_DROP_DIR" -mindepth 1 -maxdepth 1 | /usr/bin/wc -l)" = 1 || return 1
}
finch_phase() {
  finch_stopped || return 1
  if test -n "$FINCH_PHASE_SOURCE"; then
    finch_owned_dropin || return 1
    if test -n "$FINCH_INVOCATION"; then
      test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p InvocationID --value)" = "$FINCH_INVOCATION" || return 1
    fi
  else
    test ! -e "$FINCH_DROP_DIR" && test ! -L "$FINCH_DROP_DIR" || return 1
    /usr/bin/install -d -o 0 -g 0 -m 0755 "$FINCH_DROP_DIR" || return 1
  fi
  case "$1" in
    "$FINCH_AUDIT/launcher-check.conf"|"$FINCH_AUDIT/nss-tls.conf"|"$FINCH_AUDIT/metadata.conf"|"$FINCH_AUDIT/full.conf") ;;
    *) return 1 ;;
  esac
  test "$(/usr/bin/stat -c '%u:%a' "$FINCH_AUDIT")" = 0:700 || return 1
  test -f "$1" && test ! -L "$1" || return 1
  test "$(/usr/bin/stat -c '%u:%a:%h' "$1")" = 0:600:1 || return 1
  /usr/bin/install -o 0 -g 0 -m 0644 -- "$1" "$FINCH_DROP" || return 1
  FINCH_PHASE_SOURCE=$1
  # Reset only after the previous owned phase has been verified stopped.
  FINCH_INVOCATION=
  /usr/bin/systemctl daemon-reload || return 1
  finch_owned_dropin || return 1
  /usr/bin/systemctl show "$FINCH_UNIT" -p ExecCondition -p ExecStart -p Restart -p RuntimeMaxUSec -p TimeoutStopUSec -p AppArmorProfile -p NoNewPrivileges -p User -p Group || return 1
}
finch_observe() {
  local observed
  observed=$(/usr/bin/systemctl show "$FINCH_UNIT" -p InvocationID --value) || return 1
  [[ "$observed" =~ ^[0-9a-f]{32}$ ]] || return 1
  if test -z "$FINCH_INVOCATION"; then
    FINCH_INVOCATION=$observed
  else
    test "$observed" = "$FINCH_INVOCATION" || return 1
  fi
  /usr/bin/systemctl show "$FINCH_UNIT" -p ActiveState -p SubState -p Result -p MainPID -p ControlPID -p ExecCondition -p ExecMainStatus -p ControlGroup || return 1
  /usr/bin/journalctl "_SYSTEMD_INVOCATION_ID=$FINCH_INVOCATION" --no-pager -o cat || return 1
}
```

**Before every start:** independently compare the printed effective executable/
argument list with the candidate and all three original conditions. Require
Restart=no, the intended RuntimeMaxSec, infinite stop timeout, UID/GID 1000,
original profile/NNP/seccomp/capabilities and only this owned drop-in. Recheck
`finch_owned_dropin` immediately before start. Unknown settings, source changes,
other drop-ins, a different invocation or unowned control process mean HOLD.
Do not infer actual kernel enforcement solely from these settings.

### 3a. Actual launcher and import/configuration closure, zero network

```bash
cat > "$FINCH_AUDIT/launcher-check.conf" <<'UNIT'
[Service]
Restart=no
RuntimeMaxSec=120
ExecStart=
ExecStart=/home/ubuntu/.hermes/venvs/finch-mcp/bin/python -I -S -B /home/ubuntu/.hermes/scripts/finch_adapter_launch.py --check-only
UNIT
finch_phase "$FINCH_AUDIT/launcher-check.conf"
# Pause here: parent verifies the effective command/settings printed above.
finch_owned_dropin
finch_stopped
/usr/bin/systemctl start "$FINCH_UNIT"
finch_observe
```

Because Type=simple can return before ExecStart finishes, allow at most 120 seconds
for the expected output/status, repeating only the read-only observation for the
same invocation; finch_observe refuses an identity change instead of adopting it.
Missing or incomplete evidence is HOLD, not a successful start.

Expected: actual launcher output
`FULL_CONFIGURATION_READY_FRESH_CURSORS_REQUIRED_AT_START`, `network_calls=0`,
all conditions exit 0 and ExecMainStatus=0. It normally becomes inactive because
the check exits. This proves the source/clean-checkout/import/nonsecret-auth
checks under the real boundary, not live credentials, fresh cursors or gateway
readiness. `BLOCKED`, exit 78 or missing expected output means HOLD.

### 3b. NSS/DNS and certificate-verified TLS in the same boundary

This credential-free probe contacts only the existing source-bound Discord REST
and Auth0 issuer origins, after specific approval for those handshakes. It sends
no HTTP request or bearer. It uses the dedicated interpreter, clears environment
before SSL context creation (including SSLKEYLOGFILE), checks resolved addresses
are public and pins the chosen numeric address. One selected address per origin
is tested; the printed family does not establish other-family reachability.

```bash
cat > "$FINCH_AUDIT/nss-tls.conf" <<'UNIT'
[Service]
Restart=no
RuntimeMaxSec=60
ExecStart=
ExecStart=/home/ubuntu/.hermes/venvs/finch-mcp/bin/python -I -S -B -c "import os; os.environ.clear(); import socket,ssl,ipaddress,json,signal; signal.alarm(40); hosts=('discord.com','dev-qv5n6hpb2umayqwt.us.auth0.com');\nfor host in hosts:\n infos=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM); assert infos; ips=[ipaddress.ip_address(v[4][0]) for v in infos]; assert all(v.is_global and not getattr(v,'ipv4_mapped',None) and not getattr(v,'sixtofour',None) and not getattr(v,'teredo',None) for v in ips); family,kind,proto,_,peer=infos[0]; sock=socket.socket(family,kind,proto); sock.settimeout(5); sock.connect(peer); ctx=ssl.create_default_context(); tls=ctx.wrap_socket(sock,server_hostname=host); print(json.dumps({'status':'FINCH_NSS_TLS_OK','host':host,'family':family,'version':tls.version()}),flush=True); tls.close()"
UNIT
finch_phase "$FINCH_AUDIT/nss-tls.conf"
# Pause here: verify the exact fixed hosts and effective commands/settings.
finch_owned_dropin
finch_stopped
/usr/bin/systemctl start "$FINCH_UNIT"
finch_observe
```

Wait at most 60 seconds for this phase's exact output and exit status; a first
empty journal read is not success. Expected: two `FINCH_NSS_TLS_OK` records, valid certificate/hostname verification,
all conditions exit 0 and ExecMainStatus=0. Timeout, denied NSS fallback, private
DNS answer, TLS failure or missing record means HOLD, not a broad DNS/UNIX/CA
exception. This probe does not establish Discord gateway authentication,
Auth0 JWKS/identity validation or the actual tunnel peer.

### 3c. Real launcher metadata-only MCP listener

```bash
cat > "$FINCH_AUDIT/metadata.conf" <<'UNIT'
[Service]
Restart=no
RuntimeMaxSec=300
ExecStart=
ExecStart=/home/ubuntu/.hermes/venvs/finch-mcp/bin/python -I -S -B /home/ubuntu/.hermes/scripts/finch_adapter_launch.py --metadata-only
UNIT
finch_phase "$FINCH_AUDIT/metadata.conf"
# Pause here: verify exact effective commands/settings.
finch_owned_dropin
finch_stopped
/usr/bin/systemctl start "$FINCH_UNIT"
finch_observe
FINCH_PID=$(/usr/bin/systemctl show "$FINCH_UNIT" -p MainPID --value)
[[ "$FINCH_PID" =~ ^[1-9][0-9]*$ ]]
/usr/bin/grep -E '^(Uid|Gid|NoNewPrivs|Seccomp|Cap(Inh|Prm|Eff|Bnd|Amb)):' "/proc/$FINCH_PID/status"
/usr/bin/cat "/proc/$FINCH_PID/attr/apparmor/current"
/usr/sbin/ss -ltnp 'sport = :18806'
```

Require UID/GID 1000 in all slots, NoNewPrivs=1, Seccomp=2, five zero capability sets,
`finch-mcp-receiving-v1 (enforce)` and the exact MainPID owning only the intended
127.0.0.1:18806 listener. The loaded-policy identity/import/broker acceptance is
still required; a label alone is insufficient. Source output must say `starting`,
`mode=metadata-only`, protocol 2026-07-28. A healthy metadata listener does not
upgrade to full mode when a user authenticates.

Only if 127.0.0.1 is an independently verified permitted ingress peer and the
listener ownership check passes, the operator may run these local unauthenticated
probes. --disable is the first curl option so root curlrc cannot inject headers,
netrc authentication, redirects or extra URLs. Do not add a peer exception merely
to make this test pass:

```bash
/usr/bin/curl --disable --noproxy '*' --interface 127.0.0.1 --connect-timeout 3 --max-time 5 --silent --show-error --dump-header "$FINCH_AUDIT/prmd.headers" --output "$FINCH_AUDIT/prmd.json" http://127.0.0.1:18806/.well-known/oauth-protected-resource/mcp
/usr/bin/curl --disable --noproxy '*' --interface 127.0.0.1 --connect-timeout 3 --max-time 5 --silent --show-error --dump-header "$FINCH_AUDIT/mcp.headers" --output "$FINCH_AUDIT/mcp.json" -H 'Content-Type: application/json' --data '{}' http://127.0.0.1:18806/mcp
```

Expected: PRMD 200 with resource `https://hermes-01.tail9e25df.ts.net/mcp`, the
configured Auth0 issuer and Finch scopes; MCP 401 with the exact configured
resource-metadata challenge. No credentials or DB/gateway are used in metadata
mode. Do not use a failed curl's exit code as HTTP 401 evidence: inspect the saved
status/header/body. No authenticated claim follows from these probes.

### 3d. Separately approved full-mode smoke and authenticated MCP

Stop and verify this invocation has drained before changing phase. This phase
uses the existing Discord credential loader and private state through the actual
launcher; its approval must cover real provider connections and any existing
subscription behavior. No new tokens, grants, subjects, role changes or credential
entry are part of this runbook. Do not continue if those approvals are absent.

```bash
cat > "$FINCH_AUDIT/full.conf" <<'UNIT'
[Service]
Restart=no
RuntimeMaxSec=300
ExecStart=
ExecStart=/home/ubuntu/.hermes/venvs/finch-mcp/bin/python -I -S -B /home/ubuntu/.hermes/scripts/finch_adapter_launch.py
UNIT
# First perform the owned stop in section4; do not replace a live phase.
finch_phase "$FINCH_AUDIT/full.conf"
# Pause here: verify exact effective commands/settings and full-mode approval.
finch_owned_dropin
finch_stopped
/usr/bin/systemctl start "$FINCH_UNIT"
finch_observe
```

Require fresh `FRESH_FINCH_CURSORS_READ` for both fixed channels followed by
`starting`, `mode=full`. Preserve existing pending requests and receipts; current
cursor reads are not a replay or state reset. Recheck kernel identity and the
owned listener as in 3c. Repeating the unauthenticated `/mcp` probe must still
produce 401. The 300-second active ceiling begins graceful shutdown; infinite
stop timeout remains deliberate. Never add a kill timeout to finish the test.

For authenticated compatibility, the owner uses the **existing approved linked
MCP client**, which handles its own bearer securely. No shell bearer, copied
secret, new OAuth grant or reconnect is supplied here. Send only `server/discover`
and `tools/list`, with header `MCP-Protocol-Version: 2026-07-28` and this params
shape (use the second method with the same params):

```json
{"jsonrpc":"2.0","id":"receiving-discover","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
```

Expected: HTTP 200, `resultType=complete`, supported version 2026-07-28, and the
Finch tool inventory (`reply_to_finch_request`, `read_finch_messages`). Do not call
those tools, subscribe, upload or send a reply as part of this smoke.401/403 is a
real auth/scope/RBAC failure, not permission to change access. Product/tunnel
proof must use the existing verified route; do not guess a new URL or treat a
loopback response as public routing proof. A 443 socket or fresh REST cursors do
not prove gateway READY/event delivery. Any required fresh message-only ingress
proof must be separately approved and observed through the existing parent
workflow, never generated by this runbook or tied to the held report.

## 4. Owned stop, cleanup and rollback

Use after every active compatibility phase and before the next phase. First
compare the current invocation, process and exact command with the phase just
started. The following guard refuses an unexpected invocation. If a start's
outcome/identity is uncertain, obtain manual operator reconciliation; do not
populate FINCH_INVOCATION from a different process to force this guard through.

```bash
finch_owned_dropin
test -n "$FINCH_INVOCATION"
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p InvocationID --value)" = "$FINCH_INVOCATION"
FINCH_STOP_WAS_ACTIVE=$(/usr/bin/systemctl show "$FINCH_UNIT" -p ActiveState --value)
test "$FINCH_STOP_WAS_ACTIVE" = active
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ControlPID --value)" = 0
/usr/bin/systemctl show "$FINCH_UNIT" -p MainPID -p ControlPID -p ExecStart -p ExecCondition
# Pause: parent verifies these are this phase's processes, not another owner.
/usr/bin/systemctl stop "$FINCH_UNIT"
/usr/bin/systemctl show "$FINCH_UNIT" -p InvocationID -p ActiveState -p Result -p ExecMainCode -p ExecMainStatus -p MainPID -p ControlPID > "$FINCH_AUDIT/stop-$FINCH_INVOCATION.txt"
/usr/bin/journalctl "_SYSTEMD_INVOCATION_ID=$FINCH_INVOCATION" --no-pager -o cat > "$FINCH_AUDIT/stop-$FINCH_INVOCATION.journal"
/usr/bin/cat "$FINCH_AUDIT/stop-$FINCH_INVOCATION.txt"
/usr/sbin/nft --json --numeric list set inet finch_mcp_egress finch_cgroups
```

Expected: no remaining service cgroup/member, no current MainPID/ControlPID, and
all in-flight work drained. If ActiveState is inactive, `finch_stopped` must pass.
A stop hang, foreign control process, nonempty member set or missing table is
HOLD. Do not force-kill, restore a DB backup or retry an uncertain effect.

**Expected exit 2 requires explicit reconciliation, not automatic PASS/reset.**
The accepted adapter handles SIGTERM through its failed future and returns 2 after
its graceful finally/DB close. On systemd v255 this can leave a manually stopped
metadata/full phase in `failed`, `Result=exit-code`, `ExecMainCode=1` (CLD_EXITED),
`ExecMainStatus=2`. Save the evidence above first. The parent must correlate that
exact invocation's intentional stop, completed drain and successful preceding
compatibility observations. A failure before the stop, an unknown status or
`Result=timeout` from RuntimeMaxSec remains HOLD and does not qualify. Native
check-only/NSS probes are expected to exit 0; do not excuse their failures as this
adapter shutdown behavior.

Only after that exact owned exit 2 outcome has been reconciled may the operator
clear the failed state without restarting it:

```bash
finch_owned_dropin
test "$FINCH_STOP_WAS_ACTIVE" = active
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p InvocationID --value)" = "$FINCH_INVOCATION"
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ActiveState --value)" = failed
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p Result --value)" = exit-code
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ExecMainCode --value)" = 1
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ExecMainStatus --value)" = 2
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p MainPID --value)" = 0
test "$(/usr/bin/systemctl show "$FINCH_UNIT" -p ControlPID --value)" = 0
test ! -e /sys/fs/cgroup/system.slice/finch-mcp-adapter.service
test -s "$FINCH_AUDIT/stop-$FINCH_INVOCATION.txt"
test -s "$FINCH_AUDIT/stop-$FINCH_INVOCATION.journal"
# Pause: parent reconciles saved evidence; flags/exit 2 alone cannot authorize reset.
/usr/bin/systemctl reset-failed "$FINCH_UNIT"
finch_stopped
FINCH_LAST_DRAINED_INVOCATION=$FINCH_INVOCATION
FINCH_INVOCATION=
```

The historical invocation remains in the saved filenames and
FINCH_LAST_DRAINED_INVOCATION. Clearing the working variable occurs only after
this owned phase is proven stopped; it cannot turn a different live invocation
into the phase under test. Do not proceed from a timer-triggered or unexplained
failure using this block.

After the last phase, remove only the byte-identical owned override. Keep audit
files for parent acceptance. The base profile/unit/network configuration was not
modified by these compatibility phases; restoration of any older deployment is
a separate exact approval and must never restart the unconfined legacy service.

```bash
finch_stopped
finch_owned_dropin
/usr/bin/cmp --silent "$FINCH_AUDIT/base-unit.before.service" "$FINCH_BASE"
/usr/bin/rm -- "$FINCH_DROP"
/usr/bin/rmdir -- "$FINCH_DROP_DIR"
/usr/bin/systemctl daemon-reload
test -z "$(/usr/bin/systemctl show "$FINCH_UNIT" -p DropInPaths --value)"
finch_stopped
/usr/bin/systemctl show "$FINCH_UNIT" -p UnitFileState -p Restart -p ExecStart -p ExecCondition
/usr/sbin/ip netns list > "$FINCH_AUDIT/netns.after.txt"
/usr/bin/cmp --silent "$FINCH_AUDIT/netns.before.txt" "$FINCH_AUDIT/netns.after.txt"
test ! -e /run/finch-kernel-harness.lock
```

Require original base settings restored but service still disabled/stopped. Recheck
accepted nft policy fingerprint and empty membership; counters may have changed,
so raw JSON equality is not a policy comparison. Unknown changes are never
replaced automatically. Fixture-created cgroup directories must also be absent;
use the recorded fixture identities and approved cleanup procedure, not wildcard
deletes. The latest test admission remains bound to its removed cgroup; a future
approved startup must re-admit itself. Do not remove or replay durable receipts.

## Closure record and remaining gates

Record each approval reference, source/file hashes, receiving versions and boot,
phase exit/status/InvocationID, actual cgroup/nonce observations, counters and
expected-versus-observed results. Separate **not run**, **failed/HOLD** and
**passed**. No phase can silently stand in for another. Parent acceptance must
still cover actual IPv6 unconnected UDP, loaded-policy/broker/import custody,
real tunnel peer and route, authenticated product behavior, graceful single-owner
state/receipt preservation and any separately approved event-ingress proof.
Only Hermes may close deployment. No PASS here authorizes a file-review grant,
held-report access, upload, rewrite, retry, purchase or credential change.

Sources: [systemd v255 service semantics](https://github.com/systemd/systemd/blob/v255/man/systemd.service.xml),
[systemctl](https://github.com/systemd/systemd/blob/v255/man/systemctl.xml),
[Python TLS verification](https://docs.python.org/3.12/library/ssl.html),
[curl default-configuration isolation](https://curl.se/docs/manpage.html#-q).
