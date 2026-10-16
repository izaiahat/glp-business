# GLP-47 owned TEST capture adoption — U-REL02

This is a source-only implementation until independent Astra accepts the exact
PR head and the parent merges and adopts it through the registered source sync.
It does **not** enroll anyone, send mail, activate the scheduler, or close GLP-47.

## Why a fresh deployment

Pages project environment variables and a deployed function's bindings are
separate snapshots. Changing the project alone leaves the deployed capture held.
The supported path makes a **new canonical deployment using exactly the accepted
2,381-file deploy directory and compiled worker**. It never replays the consumed
same-head publication journal, compiles functions again, edits accepted history,
or points at an older release. The commit stays `40a3faf…`; the deployment ID
must change. This is settings-only in content scope, not in provider effects.

`glp_release_main.py` owns both modes under its existing main run lock and the
existing native carrier lock for mutations. No new publisher or schedule exists.
`glp_owned_test_adoption.py` is a fixed-cohort implementation, not a generic setter.
It authenticates the exact original pointer, final receipt, manifest, PREPARED,
complete release/deploy closures, compiled worker, project/account, canonical
deployment ID/commit, and both project and deployed configuration. A concurrent
release/pointer/settings change is a refusal, not a request to rebase automatically.

## Parent release procedure — only after review/merge/source adoption

Run from `/home/ubuntu/business`:

```sh
python3 -B scripts/glp_release_main.py --prepare-owned-test
```

Prepare performs authenticated GETs and writes a local PLAN only. Use its exact
`plan` and `plan_sha256`, inspect the current deployment and settings witnesses,
then apply once through the **same sole-publisher entry**:

```sh
python3 -B scripts/glp_release_main.py \
  --apply-owned-test <exact-plan-path> \
  --expected-owned-test-plan-sha256 <exact-plan-sha256> \
  --authorize-owned-test-adoption
```

Apply pins decision #10 in the operator record, requires that exact plan, reserves
a durable separate journal before any effect, and PATCHes **only**
`deployment_configs.production.env_vars.NEWSLETTER_CAPTURE_MODE` to plain-text
`owned_test`. It never round-trips secret values or replaces the project config.
Both before/after project snapshots must match except that one setting, including
preview, all other environment keys, D1, compatibility, build config and holds.
The old deployed binding must remain unchanged after the PATCH. A new direct
upload uses the exact retained `deploy-public`, `--no-bundle` and pinned toolchain.
The unique plan-bound deployment marker, exact commit, new canonical ID,
functions flag, D1, and both project/deployed capture/settings are read back.
The native route matrix, held-route runtime probes, final receipt and pointer CAS
remain required. No search notification occurs for unchanged content.

The existing provider representation is project `usage_model=standard` versus
deployed `usage_model=bundled`; both are retained separately and must each remain
unchanged. This difference is not normalized into an assumed equality.

If Cloudflare's documented partial-update behavior loses another setting, if an
upload times out, or if receiving/pointer CAS fails, the journal remains nonterminal
and blocks ordinary releases. **Do not retry a mutation or a terminal journal.**
Read the exact project/canonical deployment, journal and pointer first; an uncertain
or changed cohort requires parent reconciliation and a separately reviewed
successor, never blind patching, rollback, SQL enrollment or a substitute sender.
This intentionally trades automatic recovery for no ambiguous publication replay.

## Receiving after adoption

Require `PASS_PRODUCTION_TERMINAL`, retained native receipt, matching new canonical
ID and deployed/project `owned_test`. Resume the same ONBOARD owner for exactly one
owned TEST to the operator-owned address using the existing capture/consent path.
Read back inbox delivery and rendering, exercise unsubscribe, and authenticate
recorded consent and suppression before activation. Historical content-upgrade
consent is not onboarding permission. Do not interpret these offline tests or the
settings receipt as delivery/unsubscribe/retention/deletion proof.

## Verified in the source lane

Offline regression tests cover secret/preview/D1/other-setting loss, stale
canonical identity, exact-plan/authority guards, intent-before-effect ordering,
retained-byte upload, project-versus-deployed binding checks, native CAS gating,
ambiguous upload and terminal non-replay. A GET-only readback authenticated the
current `9dae1da7-cc07-40f5-b0a8-0719f9bb5142` deployment and found project **and**
deployed capture `held`, with the same accepted compiled worker and D1. It made
zero mutations. Live apply/send/receiving remain deliberately unexecuted here.
