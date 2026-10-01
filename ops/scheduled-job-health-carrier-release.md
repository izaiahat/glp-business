# F09 guarded carrier producer (GLP-337)

Source delivery only; independent acceptance, merge and live adoption remain
separate. `scripts/scheduled_job_health_carrier_release.py` emits **one carrier**
(`/home/ubuntu/.hermes/scripts/scheduled_job_health_manager.py`) and changes only
`b86ec8937c20.script_sha256`. No job execution, schedule change, other repin,
profile change, sends, reruns, restart or global shutdown.

## Minimum acceptance boundary: review first, short sealed cutover

PR113 preserves the original counterexamples in
`tests/test_f09_native_maintenance_boundary.py`. They remain true: the GLP44/73
maintenance flocks alone do not exclude direct carrier imports or source importers,
and the ordinary source-only cohort reopens before a subsequent native saver.
F09 no longer attempts to stretch those primitives across human review.

1. **Read-only preview and independent approval happen before exclusion.**
   A preview is an immutable byte-bound proposal, not a lease or an idle proof.
   Old code may run and inputs may change while it is being reviewed. Apply
   revalidates the five committed source bytes/modes, exact carrier predecessor,
   receipt, executing code/dependencies and **entire current target record**.
   Any change (including due cursor, optional-field presence or runtime status)
   refuses. Never reconstruct the original record from a paused current one.
2. **Apply closes maintained writers before its first input read.**
   `sync_from_repo.source_lock()` takes a nonblocking EX flock on the stable
   business-root **directory inode**, shared by `sync()` before manifest/hash
   reads and held through replacement and sync-state save. This creates no file
   during dry-run and serializes callable and CLI importers. F09 holds the same
   lock through cutover/readback. The five already-installed inputs remain
   read-only; readers of those unchanged inputs need not be stopped.
3. **Keep the target scheduled.** Use the unchanged native per-job fire fence
   and native registry thread/flock locks, in that order. Existing claims,
   running/execution owners and direct carrier process trees refuse. These are
   the scheduler's existing claim fences, not newly assumed gateway cooperation.
   There is no native pause or resume and hence no due-cursor reconstruction.
4. **Seal, then drain, then change the carrier.** The existing cohort seals the
   carrier and known bytecode before replacement, drains exact source references
   and carrier task trees again, checks for late-created bytecode, and stages the
   sole successor. New unprivileged first reads are denied even for a fresh
   `runpy` import followed by `run()`. An already-loaded carrier callable whose
   task is still alive blocks; it is never killed to clear the slot.
5. **Commit while sealed.** The new optional cohort `commit` callback runs the
   native saver and exact full-jobs readback *before* public modes reopen. Input
   CAS remains active. The immutable expected candidate is not handed to a saver
   by reference. Only after matching readback and sealed inode/content checks
   does the carrier regain its original mode. Locks release on return. This is
   restoration of admission, not a job lifecycle mutation.
6. **Uncertainty stays closed.** Durable `commit_started` precedes saver entry.
   A saver exception, bad postimage or postcommit failure retains sealed source
   and custody, writes `commit_uncertain`, emits no acceptance and never replays
   or source-only-rolls-back the save. The generic cohort recovery command
   explicitly refuses an external-commit journal. Precommit failures retain the
   existing owned-preimage rollback rules. The same cutover journal is one-shot.

This is bounded cooperative-host exclusion, **not** protection against hostile
root, capabilities, same-UID chmod/rename, arbitrary editors or deliberately
hidden in-memory consumers. No source flock can retrofit old loaded code.

## Required live bootstrap (not performed by this PR)

The source/native-fixture path is operational, but these receiving-host conditions
must be established by the existing scheduler/source-writer owner before rollout:

- Independent acceptance of the exact PR head/changed safety oracles and green
  exact-head CI; authorized merge retaining the reviewed history and a clean
  checkout of the accepted head. Invoke the producer in a **fresh interpreter**
  from that checkout. Existing GLP44/73 readiness and its frozen sources are not
  adoption evidence for F09 and are not modified here.
- **One-time importer bootstrap under the existing single-writer lane:** prevent
  new old-version CLI/callable imports, wait for every already-admitted old
  importer/editor to finish naturally, including any interpreter retaining
  `sync()`, then adopt the reviewed importer (or require all imports to invoke
  the exact reviewed checkout). Retire old resident importers/queued old commands
  before releasing that lane. From then on every maintained import must enter
  through this `sync()`/CLI. Do not assume replacing a file changes resident
  code. A directory-lock probe only proves new-version admission, not retirement
  of old interpreters. If the owner cannot account for them, **do not adopt**.
- Establish the actual carrier caller inventory: native external script launch
  plus direct CLI/runpy tasks rooted in the exact carrier path. No maintained
  production module in this repository imports and retains this carrier; tests
  do not authorize live residency. Drain any exceptional import-and-retain
  consumers (including those not attributable from argv/fds/maps) under their
  owner's existing launch hold first; retire that unsupported launch route.
  A `/proc` snapshot alone cannot prove absence of hidden cached callables.
  Do not restart the global gateway or kill an in-flight effect. New direct
  reads at the known carrier path are fenced by DAC, not a new cooperative lock.
- Run as the existing unprivileged file owner, without DAC-override capabilities,
  in the real host PID namespace. Require complete visibility of affected task
  identities/descendants and native claim/ledger state. The workspace directory
  inode must remain stable; all five inputs and producer checkout must have no
  out-of-band writers. No root reader or unrelated release is admitted to this
  bounded cutover. Existing source adapters that touch other scopes remain held
  separately; there is no whole-host idle prerequisite.
- Preinstall the **five** reviewed inputs through the admitted scoped importer,
  with dry-run → apply → dry-run (`diffs=0`): carrier snapshot, drift checker,
  inventory, budget and transport. Importer/dependency source is not a sixth
  emitted carrier input; the producer executes its helpers from the exact clean
  checkout. Do not import these inputs during cutover. Resolve historical
  baseline refusals through supported `--adopt-reviewed-baseline`, never force
  copy or edit sync-state. Keep the actual target job enabled/scheduled and
  claim-free; if a prior maintenance window owns a pause, its owner must restore
  that exact preimage first. Do not use ordinary resume to manufacture eligibility.

These are scoped bootstrap/adoption prerequisites, not missing source APIs or
permission for this source-only lane to execute them. The carrier DAC fence
covers the existing predecessor without installing reader cooperation first.

## Independent receipt and custody

Receipt schema remains `scheduled-job-health-carrier-review-v1`, with
`decision=REVIEW_ONLY` for preview, different nonempty `author`/`reviewer`, UTC
`reviewed_at_utc`, nonempty `evidence`, and `writer_exclusion` citing the actual
bootstrap/caller inventory and receiving-side owner (not merely “idle”). Include
`producer_head`, reviewed ancestor `source_head`, fixed `predecessor_sha256`,
actual decimal `predecessor_mode`, complete `expected_job`, and these exact keys
in both `source_sha256` and `source_mode`:

- `ops/hermes-cron-scripts/scheduled_job_health_manager.py`
- `scripts/scheduled_source_drift.py`
- `ops/scheduled-source-drift-manifest.json`
- `ops/n8n_execution_budget.py`
- `ops/n8n_dt.py`

The fixed predecessor is
`d652c1d7bd3b6968af117d36ebbdd39ce119aa6c9b0fcadb0f0fd79191370dc9`.
All source hashes must match committed reviewed bytes and preinstalled inputs;
all modes must match actual independently reviewed permission bits. The entire
producer dependency set in `CODE` is authenticated against `producer_head`.

Preview (after bootstrap/source preinstallation, not under a paused window):

```sh
python3 "$REPO/scripts/scheduled_job_health_carrier_release.py" \
  --repo "$REPO" --review-receipt "$REVIEW"
```

The independent reviewer preserves the preview, checks its emitted hash, and
sets `decision=ACCEPT_EXACT_CURRENT_BYTES` plus `proposed_sha256`. Apply the same
command with `--apply` **once**, while the complete record is still current.
Any intervening tick/source/mode change requires a fresh independent review,
not a weaker CAS. The author cannot issue acceptance. Receipt changes are
rechecked before the native commit; target and unrelated records are read back
exactly. Preimages/stages and `cutover.json` remain under
`reports/ops/pin-archive/scheduled-job-health/<review-sha256>/`.

On `commit_uncertain`, preserve custody and sealed files, inspect the exact
native registry outcome and callback journal under owner control, and obtain
independent recovery authorization. Do not retry apply, manually reopen, run
ordinary cohort recovery, pause/resume or overwrite the registry to make it
match. A process/power loss can leave the seal in place even if the final receipt
was not written; absence of `acceptance.json` is not evidence nothing happened.

After authorized success, verify only the approved hash changed, due/optional
fields stayed exact, source modes restored and inputs stayed unchanged. Run the
registered **read-only** `scheduled_source_drift.py` checker, retaining factual
CLEAN/DRIFT/UNKNOWN and nonzero checked counts. Never run the health manager to
prove this branch: its existing alert/rerun effects remain separately held.
Natural first-tick/delivery evidence is separate from this offline source proof.

## Transport/source provenance retained

`ops/n8n_dt.py` remains byte-for-byte the existing host predecessor, SHA256
`3c029b4441de0928dca4294077c9a351b4c3b17d50969cc3b4ee84ffbce5569c`, observed
mode `0644`. No transport rewrite, credentials, environment/state capture or
broad ops import. `health_check(get=None)` imports `_native_http` before its
cache path; that dependency is required even on a cached tick. The transport
imports only standard-library modules. Existing manager transports/holds stay
unchanged.

All 34 host-cron inventory rows use `{id, surface, sources, payloads}`; paths are
in `sources[].path` (14 one-source, 20 two-source). The existing drift fix and
row-local UNKNOWN checks are unchanged by this cutover repair.
