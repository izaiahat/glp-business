# Awin fresh preflight — paused source candidate (GLP-317 / K10-035)

This replaces the **preflight caller**, not any application pipeline. Source-only;
not installation, activation, approval or a new application queue. The existing
`glp_awin_portal_login.py` PR #17 caller is retained, with exact directory-path
and protected-directory readiness checks. API success cannot prove portal health.

## Adoption boundary

- The brief names `7a2692ad0639`. It was **absent** from the default-profile
  registry during preparation. The observed matching paused job is `7a269c665b12`,
  `GLP Awin auth liveness preflight`, script `awin_auth_liveness_preflight.sh`.
  The parent must resolve this identity mismatch before any registry update.
  Never search other profiles or retarget by name alone.
- `ops/awin-fresh-preflight.candidate.json` is a review packet, NOT a scheduler
  import or authority to apply. It contains a paused-only proposed delta. Never
  pass the complete packet to `update_job` or edit live jobs.json by hand.
- After exact-head independent review, authorized merge and maintained-source
  adoption, independently verify the candidate JSON's actual SHA-256 against
  the reviewed PR. The proposed job prompt binds the canonical source pin-set
  digest (SHA-256 of sorted compact JSON). Verify that digest before importing
  source, then verify every `source_sha256` entry. Refuse missing/mismatched
  pins; do not repin during a run.
- Review the actual scheduler's agent/tool availability, source and model
  routing, output declaration, shared admission and vault handoff in an isolated
  scheduler fixture before adoption. Preserve schedule, next_run_at, delivery,
  pause reason, counters and unrelated record fields. Read back the complete
  record still paused/disabled after separately authorized adoption. Activation
  and first registered execution require a separate decision; no manual trigger
  or unpause is authorized by this packet.
- An agent/tool job is required: `no_agent=false`, `script=null`. The old shell
  entry point cannot call the vault tools. Do NOT add an unattended password
  adapter, import `portal_apply`, launch a nested agent from the shell, or invoke
  `glp_awin_portal_drain_cron --auth-preflight-only` as fallback. The old wrappers
  and runtime pins remain untouched historical/held paths, not dependencies of
  the replacement. No claim of repaired old runtime guards is made.
- Keep `52e2014a9579` and `5a2cba16e057` retired. Keep the CDP keeper and all
  application/drain holds intact. Do not change fit/model/credential policies,
  Fella exclusion, provider holds, application authority or Sheet gates.

## Bounded execution instructions for the adopted agent job

Skills: credential-boundaries, headless-portal-apply-sessions, host-ops-cron-systemd.
Use `openai-codex` / `gpt-6-astra-900k`, normal medium reasoning. No model/provider
fallback, delegation, paid route or capacity bypass. Only authentication and
read-only publisher/directory checks are permitted; no applications, joins,
terms acceptance, sends, tracking clicks, credential changes or account edits.

1. Validate the adopted source pin-set digest from the job prompt, then every
   exact source file digest. Run from `/home/ubuntu/business`:
   `python3 -m scripts.awin_fresh_preflight`.
   This calls the maintained Awin API adapter for **one GET** of publisher
   `2979387`'s joined programmes, rejects redirects and malformed/unsuccessful
   responses, and prints only a sanitized summary. Source failure (exit 66)
   stops the run. API `UNAVAILABLE` permits browser fallback, not credential
   extraction/creation or a manufactured API PASS. API `PASS` is only API health;
   the portal still needs its own exact identity and protected-page readback.
2. Acquire the existing heavy/browser lock using
   `python3 scripts/candidate_build_lock.py acquire --seat awin-fresh-preflight-<run-id> --reason 'Awin read-only liveness'`.
   On contention stop/defer; do not steal locks or kill a holder. Keep any
   existing host resource/admission guards. Release only this owned lock in
   final cleanup. A unique run id must never reuse a prior browser session.
3. Start a **new named AI-browser session** `awin-preflight-<run-id>` with
   `new_tab(login.PRELOGIN)` and `wait_for_load()`. No persistent profile,
   imported cookies/storage, fixed CDP endpoint or Playwright portal transport.
   Reuse this exact named session for all remaining browser/vault stages.
4. In `browser_exec`, import the adopted `scripts/glp_awin_portal_login.py`
   by exact path; invoke `login.prepare(page_info, js, click_at_xy, cdp,
   wait_for_load)`. It enters only the nonsecret identifier and must stop at
   `vault_fill_required` on `https://id.awin.com`. Never print raw page info,
   auth URLs, body text, form values, cookies, exceptions or screenshots.
5. Invoke **browser_vault_list**, select the login bound to
   `https://id.awin.com` with identifier `ops@glpclinicscout.com`, then invoke
   **browser_vault_fill**. These are actual tool calls outside Python; source
   must never read/return/type passwords. If missing, use the vault's secure
   save UI; refusal stops. Origin mismatch stops; do not use the ui.awin.com
   item on id.awin.com. A login form itself is not a blocker.
6. In the same named session invoke
   `login.finish(page_info, js, wait_for_load, goto_url)` exactly once after
   successful vault fill. Catch `LoginBoundaryError` as its fixed code only;
   redact all other exceptions. If a genuine MFA code challenge is observed,
   call **browser_vault_enter_code** with the selected handle, then use
   `login.verify_directory(...)`. Stop on unsatisfied device/passkey/app approval;
   no password retry, guessing, direct code input, reset or alternate account.
7. A fresh navigation may finish before the directory hydrates. Only on
   `directory_controls_not_ready`, recheck using
   `login.read_directory(page_info, js)` up to **two** additional times, with
   a bounded two-second foreground wait between reads. This function performs
   no navigation, input or clicks. Never rerun `prepare` or `finish` as a retry.
   Origin/account mismatch or a still-unready page remains blocked.
8. Success requires the actual caller result: publisher `2979387`, account
   `Tilton Group`, exact protected directory URL, `authenticated=true` AND
   `directory_ready=true`. Store the sanitized API result and actual browser
   result with UTC time, run/session identity and caller digest in a private
   per-run receipt. Do not synthesize HTTP status, use old receipts, or equate
   tests, an API PASS, a page title or HTTP 200 with portal success.
9. Leave the owned tab at `about:blank`; use only supported owned-session cleanup,
   never export session state or operate a persistent listener. Release the
   heavy/browser lock. Output `SILENT` only after the successful current receipt;
   otherwise one existing ops-alerts card with fixed failed stage, reason and
   private receipt path. No additional sends or new notification routes.

## Evidence limits

Unit fixtures are not provider evidence. The implementation report records the
separate real API read and fresh browser/vault run, including the initial
hydration refusal and the same-session protected-directory readback. No
registered cron execution, source deployment or schedule change is claimed.
Both unchanged API dependencies (`marketplace_adapters/awin.py` and `_base.py`)
are curated here to make this small caller importable in a clean checkout;
only the official readback method is invoked, never apply/linkbuilder/portal.
