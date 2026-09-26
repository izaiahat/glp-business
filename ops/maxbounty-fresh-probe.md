# GLP-318 fresh capacity producer — candidate, not activated

**No operational use before independent review and a separately authorized owner-admitted preflight.** No live MaxBounty request was made to develop this candidate. API availability, account entitlement and the documented account-wide capacity contract remain **UNKNOWN**. The observed private campaign response is observed only as part of normal browser navigation; this is not a supported publisher-API claim or a direct endpoint client.

## What changed

`glp_maxbounty_capacity_monitor.fresh_browser_session()` holds the existing application/MaxBounty leases and persistent-profile ownership refusal, verifies source pins, creates an in-memory `FreshCapacityProbe`, and returns its lease stack and producer. The same `run_once(state, read_queue, producer.probe)` seam consumes the result. No changes to the queue selector, Fella exclusion, state classifier or old reuse-only oracle. The unattended `live_probe()` no longer launches the cookie-only persistent driver; it retains lock/profile refusals and stops at `FRESH_AI_BROWSER_TOOL_OWNER_REQUIRED` if an owner has not supplied the staged tool session.

The producer creates only a new incognito browser context, guards the blank target before navigation, observes one normal authentication response, and checks a unique rendered protected-page email rather than `session.email`. The exact account is `ops@glpclinicscout.com`. Both before and after the detail read the principal must match; a visible login/challenge fails. No fields or credentials are read from the vault, filesystem, storage or browser form. Fresh authentication is named `fresh_authorized_login`, never `session_reused`.

Only `m2b-maxbounty-14449`, campaign `14449`, and `https://affiliates.maxbounty.com/campaign/14449` qualify. The normal campaign response is correlated by the actual paused CDP request/session/main-frame ID. Budget is consumed before dispatch, and a timeout or dropped response does not reset it. No reload, retry, direct fetch, bulk directory read, tracking click/mint or approval action. The original one-authentication/three-pageView limits are retained; unexpected MFA endpoints are **not** automatically allowed.

200 plus the exact existing capacity marker yields BLOCKED. Valid markerless pending/denied/apply/approved results remain UNKNOWN. Valid 404 stays target-unavailable/UNKNOWN. Hashes retain decoded-response-text UTF-8 semantics. Only sanitized projections survive in memory; no raw response capture, credential store, signed receipt or success-receipt importer exists. The existing state writer is unchanged, and tests read its saved JSON back locally.

## Helper trace (read source; no browser started)

- Hermes `tools/browser_use_cli.py`: `browser_exec(code, session, timeout_s, task_id, local)`; named-session routing and `_attach_vault_supervisor`. It binds the final actual target ID to the same task's vault supervisor. A named session alone is **not** proof of freshness or exclusive ownership.
- Installed `browser_harness/helpers.py`: `cdp(method, session_id=None, **params)`, `drain_events()`, `switch_tab(target, activate=False)`. These exact callables are injected, with no invented adapter methods. Raw Target commands are browser-scoped in the inspected daemon.
- Installed `browser_harness/daemon.py`: events have `method`, `params`, `session_id`; `drain_events` empties a `deque(maxlen=500)`. A full batch therefore refuses instead of pretending complete evidence.
- Hermes `tools/browser_vault_tool.py`: existing list/fill/enter-code handlers and exact-target supervisor binding. They remain explicit agent tool boundaries, not Python password access. No new vault implementation or automatic credential loader.
- Existing MaxBounty adapter: relationship projection, 500-character normalization, capacity/pending/denied predicates, and the exact `Request approval to run` / `Build tracking link` labels. The producer uses these labels only for read-only classification; never clicks either.

## Remaining adoption blockers (do not bypass)

1. **Tool-owner lifetime:** `browser_exec` creates a fresh Python interpreter on each call. It does not preserve this controller or its file-lock handles across separate vault calls. A maintained, independently reviewed in-process tool-owner bridge must retain the lease/controller across the explicit vault/MFA boundary. This PR does not invent a daemon, pickle/state store, signed intake receipt or new tool API to hide that gap. The paused cron is not an authenticated tool owner.
2. **Actual browser boundary proof:** `Target.autoAttachRelated`, Fetch request/response interception, frame IDs, response IDs, unsupported child/worker refusal, event-loss behavior and non-HTTP transport containment need independent backend verification before any credentials or portal requests. CDP fixture tests are not proof of WebRTC/network containment. Unknown enforcement stops before live use.
3. **Portal principal/contract proof:** no live portal selectors or principal DOM were examined. If the protected DOM does not expose a unique account email, the candidate refuses. The restricted asset/auth allowlist may also refuse required resources/MFA. Do not broaden it by guessing or equate configured login email with the authenticated principal.
4. **Reviewed pin closure:** live pins remain untouched. The source factory additionally requires pins for both the monitor and new producer. Reviewed rollout must retain the original closure and bind the actual helper/runtime dependencies; offline manifest integrity is not installed adoption. No automatic repin or scheduler change.
5. **Admission/review:** parent coordinates the single heavy-browser slot after the Awin owner releases it. An independent reviewer must accept the candidate and resolve the above gaps. No preflight, unpause or hold release is authorized by this document.

## Offline verification

`python3 -m pytest -q tests/test_glp_maxbounty_fresh_probe.py`

Tests call the real factory, real leases/source-pin validation, real `run_once`, and real fresh producer. Only CDP transport/rendered DOM/authentication events are fixtures. Required positive blocked, wrong-principal and marker-absent cases are exercised without a fake preclassified `probe` result. Additional cases cover timeout budget, duplicate request, HTTP errors, body identity, fresh auth versus cookie reuse, lock/pin refusal, malformed queue identity and blocked effect URLs. No live result is inferred.

The retained operational receipt remains `BLOCKED_READ / PROFILE_ALREADY_OWNED`, capacity UNKNOWN, successful reads 0. No queue, Sheet, tracker, holds, cron, current pins or persistent-profile bytes are changed by this PR.
