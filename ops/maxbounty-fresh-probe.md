# GLP-318 fresh capacity producer — draft, not activated

**No operational use before independent review and separately authorized adoption.** No live MaxBounty request, login or preflight was made for this revision. API availability, account entitlement and account-wide capacity semantics remain **UNKNOWN**. Observing the private campaign response during normal browser navigation is not a supported publisher-API claim.

## Proven gap and bounded repair

PR #17's `scripts/glp_awin_portal_login.py:34–56,59–87` implements stateless `prepare()` → actual origin-bound vault tool → re-imported `finish()`. Python interpreter lifetime alone does not require a new bridge. PR #33's retained evidence records publisher `2979387` login/readback; this revision does not rerun it. That pattern requires an agent/tool job, not a standalone password-loading shell.

At PR #34 head `975a3b66dd6ad51e9961f9849f3007b1806c7c67`, `scripts/glp_maxbounty_capacity_monitor.py:231–249` holds the original application and MaxBounty kernel locks in an `ExitStack`; `fresh_browser_session()` at lines 252–273 retains it only during the producer's interpreter lifetime. `test_real_session_leases_do_not_survive_interpreter_exit` proves with real processes and flock that another owner can acquire after interpreter exit. Reacquisition in the next stage is not continuous custody. The low-level kernel-lock function and this baseline regression remain intact.

This revision adds **durable single-run custody for cooperating capacity-probe stages**, not a working resumable browser controller. `capacity_lease_stage()` wraps both original locks, verifies/renews an atomic lease, and retains it across interpreter exits. The existing factory and fail-closed live entry also honor it, so they cannot bypass a staged run's reservation.

## Stage lease contract

- Fixed private runtime path: `reports/glp-clinic-scout/affiliate-machine/applications/.maxbounty-capacity-lease.json` beneath `BUSINESS`. No runtime lease is committed or installed.
- Start: `with capacity_lease_stage() as token:` creates a random 256-bit owner token and active expiry. Retain the token privately for the next interpreter; do not put it in reports, command-line arguments, URLs, browser state or portal requests. It is a local custody capability, not a login credential.
- Resume: `with capacity_lease_stage(owner_token=token):` requires the existing active, unexpired lease and matching token. Every stage verifies and renews on entry and normal exit. Missing, malformed, released, expired or wrong-owner leases refuse before the stage body. A missing file permits only a new start, never a resume.
- Final: `with capacity_lease_stage(owner_token=token, final=True):` verifies/renews normally, then writes a released tombstone after successful cleanup. It does not unlink the lease or guard locks. Release permits a new start with a new token; the previous token cannot resume. Exceptions/crashes retain the active reservation until expiry. The single-process factory uses a final stage and closes its owned browser before releasing custody.
- Expiry: default 300 seconds; explicit finite positive TTL must be at most 900 seconds. UTC epoch timestamps use the trusted host wall clock. An observed clock regression refuses. There is no heartbeat, daemon or automatic recovery. A stage overrun cannot renew/release an expired lease; stop rather than reset identity or budgets. Admission does not authorize effects beyond expiry.
- Takeover: only a **new** start after expiry can replace an active reservation, and only after obtaining both original kernel locks. Even an expired lease cannot be taken over while another stage still holds those locks. The new owner, former-owner SHA-256 references, old expiry and takeover timestamp are persisted together in the atomic replacement. Takeover history survives release and later starts. A stale former owner cannot renew or release the successor.
- Serialization: the original `.application-machine.lock` and MaxBounty `owner.lock` are acquired nonblocking in the original order and held throughout each stage. They are never replaced/unlinked. **Never flock the lease JSON itself:** it is replaced atomically, so its inode is not a valid mutex. Lease writes use the existing same-directory temp file, mode 0600, file fsync, rename and directory fsync. Nonregular/symlinked or malformed lease files refuse; they are not automatically repaired.

The original profile-idle check runs on every stage. Browser/heavy admission remains a separate prerequisite. The durable token fences **cooperating capacity callers**; it cannot keep a kernel flock alive after interpreter exit or fence unrelated legacy writers that ignore the lease. Adoption must keep such writers excluded across the vault interval and review clock/expiry policy. Expiry is not permission to manipulate another browser or retry an uncertain effect.

## Remaining guarded-request integration blocker

`scripts/glp_maxbounty_fresh_probe.py:58–69` retains context/session IDs, authentication observation and consumed-attempt counters in memory. `begin()` at 81–118 requires an exclusive blank context and installs Fetch interception before navigation. `poll()` at 162–215 actively drains paused requests and consumes budgets. `probe()` at 252–299 requires the same authenticated controller. Re-importing cannot recover that evidence; rerunning `begin()` refuses a nonblank browser. Moving interception after vault login or reconstructing success from configured identity would weaken existing guards.

**This lease change does not solve that separate constraint.** The live entry remains `FRESH_VAULT_STAGES_NOT_INTEGRATED`; it never falls back to the retired persistent profile. There is no new generic bridge, daemon, credential adapter or receipt importer. A future staged runner must integrate the lease checks with the actual vault/MFA boundaries and preserve request-state evidence before any operational use. A lease-only test is not proof that a vault tool call or browser action is fenced.

## Preserved capacity behavior

- Exact queue `m2b-maxbounty-14449`, campaign `14449`, detail URL and Fella exclusion unchanged.
- Existing source pins and protected rendered account identity remain mandatory; configured email is not proof. No live pin, scheduler, hold or registration change here.
- One campaign dispatch with budget consumed before response; no retry/reload. Existing authentication/pageView limits, prohibited tracking/approval/bulk reads and fail-closed response validation are unchanged.
- Exact capacity marker means BLOCKED. Absent marker, pending/denied/apply/approved results and valid target-unavailable 404 mean **UNKNOWN**, never AVAILABLE. Pending-request count remains UNKNOWN.

## Offline verification

Run `python3 -m pytest -q tests/test_glp_maxbounty_fresh_probe.py tests/test_glp_awin_portal_login.py` and the curated `bash scripts/run_tests.sh`. Both PR workflows include the stacked PR's base branch and check out the exact PR head, rather than an unverified synthetic merge.

The caller-bound browser fixtures exercise real factory → pins/leases → `run_once` → producer. Only CDP/DOM/auth events are fixtures, not preclassified receipts. Existing wrong-principal, absent-marker, duplicate/forbidden request and controller-reconstruction UNKNOWN regressions remain.

New lease regressions use real independent interpreters and real flock against isolated temporary paths: simultaneous starts have exactly one winner; a finished start still excludes another owner; resumed stages renew; wrong tokens refuse without changing bytes; final release enables a new run; actual elapsed expiry permits an audited takeover; former owners cannot renew/release a successor; both original locks exclude contenders even past expiry while a stage is active. Malformed/missing lease and invalid input regressions fail closed. The factory cannot bypass an outstanding staged lease and releases after owned-browser cleanup.

Fixtures do not prove live login, vault target integration, backend-wide network containment, or independent acceptance. The historical operational receipt remains `BLOCKED_READ / PROFILE_ALREADY_OWNED`, capacity UNKNOWN, successful reads 0. No browser, live portal effect, source install, live repin, scheduler change, application, queue/Sheet write, merge, deployment, unpause or hold release is authorized by this draft.
