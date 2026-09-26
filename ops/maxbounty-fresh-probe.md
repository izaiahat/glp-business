# GLP-318 fresh capacity producer — draft, not activated

**No operational use before independent review and separately authorized adoption.** No live MaxBounty request, login or preflight was made for this revision. API availability, account entitlement and account-wide capacity semantics remain **UNKNOWN**. Observing the private campaign response during normal browser navigation is not a supported publisher-API claim.

## Awin reuse finding: concrete compatibility blocker, not a bridge requirement

PR #17's `scripts/glp_awin_portal_login.py` **does work without retained Python state**: `prepare()` (lines 34–56) validates the login stage, enters only the nonsecret identifier and returns `vault_fill_required`; `finish()` (59–87) independently inspects the same browser after the real origin-bound vault tool fills it. A fresh interpreter can re-import the module. The browser, not a Python controller, holds the login form. This is the preferred login pattern; interpreter lifetime alone is **not** a reason to build a bridge.

PR #33 reused that pattern and its retained report records the real publisher `2979387` login/readback. Its source at `af93a1f4f70b0182343ee46424d12590f3e78125`, `ops/awin-fresh-preflight.md:31–34,63–86`, explicitly requires an **agent/tool job**, with actual vault calls outside Python. It is not a standalone password-loading script or proof of registered unattended cron execution. The present revision neither reruns nor disputes that successful Awin login.

A direct transplant into this MaxBounty candidate is blocked by two specific differences:

1. **Existing session-wide kernel leases.** `scripts/glp_maxbounty_capacity_monitor.py:231–249` opens the original application and MaxBounty lock files and holds `flock` handles in an `ExitStack`. `fresh_browser_session()` (252–273) owns that stack through browser startup, auth and the guarded probe. This is not a new lock policy: at base commit `fad3759c1fe0b45742ddabfc3f76e19c1d20afc4`, the original `live_probe()` also held those locks through driver close (source lines 231–263). Awin's staged module has no equivalent file handles. Returning from a Python stage closes the handles; reacquiring for `finish()` permits another owner in the gap. Holding only the separate heavy/browser admission lock does not replace either original lease.
2. **Candidate-only controller state and interception.** `scripts/glp_maxbounty_fresh_probe.py:58–69` owns context/session IDs, auth observation and attempt counters in Python; `begin()` (81–118) requires an exclusive blank browser and installs Fetch interception before navigation. `poll()` (162–215) actively continues paused requests and consumes the one-attempt budget; `probe()` (252–299) requires that same authenticated controller. Re-importing a new controller after vault fill does not recover this evidence. Calling `begin()` again refuses the now nonblank browser. Reconstructing counters from configured identity or labeling existing cookies a fresh login would falsify evidence. Moving all interception after an unguarded `finish()` would drop the existing before-network request boundary.

The installed tool trace is concrete: `/home/ubuntu/.hermes/hermes-agent/tools/browser_use_cli.py:633–635,658–682` clears the vault target before execution and binds the returned actual target **after** the CLI call finishes; lines 715–719 document fresh interpreters. A concurrently waiting helper is therefore not a drop-in use of the proven Awin sequence either. This describes the inspected runtime, not a claim that no other supported design can exist.

**Result:** the prior generic requirement to create a maintained tool-owner bridge is withdrawn. No bridge, daemon, lease store, receipt importer or new credential path is prescribed or added. However, **working MaxBounty Awin-pattern integration has not been delivered**. The current draft remains fail-closed at `FRESH_VAULT_STAGES_NOT_INTEGRATED`. Parent/reviewer must select a design preserving continuous existing lease custody and the guarded request budget before replacing this candidate. Do not silently narrow the lease lifetime or broaden network authority merely to call the migration complete.

## Preserved candidate behavior

- Existing `run_once(state, read_queue, producer.probe)` seam, exact `m2b-maxbounty-14449`, campaign `14449`, and `https://affiliates.maxbounty.com/campaign/14449` remain unchanged.
- A fresh incognito context only; no persistent profile or imported cookies. Existing profile-ownership refusal remains before browser calls.
- Require protected rendered account evidence for `ops@glpclinicscout.com`, not configured email. Wrong account, login wall or challenge refuses. A future Awin-style login must use the actual vault list/fill tools bound to **`https://affiliates.maxbounty.com`**, never the Awin item, password files or direct secret typing. This binding has not been exercised on MaxBounty.
- One campaign dispatch, budget consumed before response, no retry/reload. Normal auth/pageView limits and blocked tracking/approval/bulk reads retained.
- Exact capacity marker means BLOCKED. Markerless pending/denied/apply/approved results and valid target-unavailable 404 mean **UNKNOWN**, never AVAILABLE. Decoded response-text UTF-8 hashing is retained; only sanitized projections survive.
- Pin validation for monitor and producer, queue selector, state writer, Fella exclusion and holds are unchanged. The retired persistent driver is not called as fallback.

## Offline verification

`python3 -m pytest -q tests/test_glp_maxbounty_fresh_probe.py tests/test_glp_awin_portal_login.py`

The original caller-bound fixtures still run real factory → leases/pins → `run_once` → producer. CDP/DOM/auth events alone are fixtures, never a preclassified probe receipt. They cover blocked, wrong principal, absent marker, failed response, consumed timeout, duplicate request, wrong campaign, reused context and forbidden URLs.

Added compatibility regressions:

- Actual Awin `prepare` → fresh module import → `finish` succeeds against the retained browser fixture, without a retained controller. This is fixture proof only, distinct from PR #33's real login evidence.
- A reconstructed MaxBounty controller fails through real `run_once`, with UNKNOWN and zero successful reads. It does not import success from a prior controller.
- A real short-lived Python subprocess owns the **real `session_leases()` implementation against temporary paths**. A second owner is refused while it lives, then can acquire both leases after interpreter exit. No live lock, browser, credentials or network involved. This proves the inter-stage custody gap rather than merely asserting it.

Fixtures do not prove actual CDP child/worker/frame containment, vault target integration or rendered MaxBounty principal availability. These remain independent-review/adoption questions, not grounds for changing guards in this source-only task. Reviewed complete source/helper pins are required before later use; none were adopted here.

The historical operational receipt remains `BLOCKED_READ / PROFILE_ALREADY_OWNED`, capacity UNKNOWN, successful reads 0. This revision does not rewrite it. No scheduler, registration, queue, Sheet, tracker, holds, live pins, installed caller or persistent profile is changed. Keep this PR draft; no merge, deployment, unpause or hold release.
