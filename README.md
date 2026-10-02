# GLP business control-plane source

Private, curated code/instructions snapshot. This is not a copy of the live business workspace or a source of private runtime data. `ops/rebuild-source-manifest.json` is the explicit file list and source-hash ledger. It includes direct live cron entry scripts, native Hermes cron wrappers (under `ops/hermes-cron-scripts/` for review, **not** automatically installed into `~/.hermes/scripts`), selected regression tests, operator instructions, and reviewed policy registries. It excludes live scheduler configuration/state, secrets, raw portal exports, private affiliate URLs, SQLite, reports/history and the site candidate tree. Separate source systems remain prerequisites for full runtime restoration.

## Changes and installation

1. Fetch `main`, create an isolated worktree and branch, edit a scoped set, update manifest hashes/count for every changed tracked file, and run `bash scripts/run_tests.sh` plus `gitleaks git --redact --no-banner .`.
2. Push a PR. Do not merge until targeted CI and appropriate independent/approval gates pass on the final head. This repo does not assert branch protection or authorized hosted Actions minutes; check billing/protection before relying on automation. A merge is not a release or spend authority.
3. From the live workspace, run `bash scripts/sync_from_repo.sh --dry-run`, then `bash scripts/sync_from_repo.sh --apply`. It fetches private `main`, refuses local edits to curated files, and copies only the allowlist. It does not restart crons or deploy a site. Native Hermes wrappers require a separate reviewed installation plan; a sync does not touch `~/.hermes/scripts`.

If the initial live workspace contains a curated file that differs from remote, sync refuses to overwrite it. Reconcile the delta into a PR or deliberately restore the reviewed bytes first. State is kept outside Git at `ops/.rebuild-sync-state.json`.

## GLP331 dependency-only generation

`glp44_scoped_adoption.py --generation glp331-dependencies-20261001` supports
only the PR127 dependency blobs from immutable commit
`0b61c25a7177896369f4fa76b4e84192de6bee70`. It switches exactly the backlog
worker, GO refresher and sync state through the maintained journal/cohort switch.
It does not install a carrier, repin a job, execute a worker or consume an event.
Both installed predecessors must match the literal hashes and mode `0644`.
The importer preview also authenticates the refresher's committed predecessor;
it is never used to perform the switch.

This generation requires the same verified active window for preview and apply,
with a fresh `glp44-scoped-adoption-review-v1` ACCEPT_EXACT_BYTES receipt and
matching `glp331-dependency-runtime-review-v1` proposal-hash acceptance. Both
reviewers must differ from `prepared_by`; fixture labels are not real approvals.
The proposal binds producer HEAD/bytes, immutable candidate manifest, exact
source hashes/modes, full state preimage/predicted after hash and two-entry delta,
installed global manifest, complete paused registry and active journal preimage.

Complete any earlier state-changing cohort (including GLP73 when sharing the
window), then preview/review/apply GLP331 dependencies and verify the applied
transaction. **Only afterward produce the PR71 GLP44 preview**, obtain its own
fresh independent acceptance and apply in the same journal without intervening
source/state mutation. An earlier PR71 state preimage is stale. Default PR71 and
PR119 reporting behavior is unchanged.

Keep competing source writers, retained/old importers, manual callers and cloud
wait/resume admission excluded throughout review/publication. The source lock
serializes maintained importers during publication; it is not continuous
exclusion for retained code or hostile same-UID/root writers. The intentionally
stale reconciliation carrier gates still require a separate reviewed cutover or
explicit safe fenced handoff before allowing a natural run. This source change,
its CI and the disposable-root test confer no live-window or release authority.

## GLP44 collector publication successor

`glp44_scoped_adoption.py --generation glp44-collector-successor-20261002`
publishes the reviewed hourly collector (`af1d8f9…`, PR142 + PR146) as a
supported member instead of only a PR119 prerequisite. It stages the PR71
members plus the PR119 reporting members, all from immutable merge commit
`488eeffd8a01f2fdb2fb19abf95636ad33829851` with literal per-file hashes, so the
staged wrapper, reporting pin manifest and collector are one closed generation.
The unchanged reporting pin closure still requires every retained host-only
pinned leaf to match. PR71 is unchanged and still stages old collector
`24f3798…`. Review/apply require the same verified active window, distinct
`prepared_by`/`reviewer`, and a `glp44-collector-successor-runtime-review-v1`
proposal-hash acceptance. Use this instead of the PR71 then PR119 sequence when
PR71's pins no longer close on the receiving host.

## GLP337 fixed shared-bridge generation

The existing scoped adopter's `--generation glp337-shared-bridge-20261001`
materializes only immutable independently reviewed PR132 commit
`898b45e903dffcce4046aa5548fc3e6f23b60b78`, and journals/switches exactly
`scripts/cron_bridge_action.py`, `scripts/glp_analytics_brain_cadence.py` and
`ops/.rebuild-sync-state.json`. Resident source hashes must be the literal
`f00f667…` / `61ca597…` pair in mode `0644`; targets are fixed `f1055de…` /
`4d51a62…`. No caller-supplied path/hash override exists. The no-apply PR132
importer handoff requires the retained launcher `4e8da70…` baseline and either
an absent cadence baseline (authenticated through Git history) or `61ca597…`.
It never performs publication; the maintained active-window cohort switch does.

Preview and apply both require a verified active journal. A fresh
`glp44-scoped-adoption-review-v1` / `ACCEPT_EXACT_BYTES` receipt binds every
proposal field, with nonempty distinct `prepared_by` / `reviewer`; its
`runtime_review` must have schema `glp337-shared-bridge-runtime-review-v1`,
`ACCEPT_EXACT_BYTES`, an independent reviewer distinct from the preparer and
SHA256 of the exact proposal serialized as `json.dumps(proposal, sort_keys=True)`.
The proposal binds producer HEAD/bytes, immutable manifest, both source modes,
full sync-state before/after hashes and two-entry delta, complete paused native
registry and active journal, installed global manifest and retained drift
inventory/actions/bridge bytes and modes. Existing keeper/source fences, all
paused Hermes fire fences, registry fence, importer source lock, durable custody,
CAS and the maintained uncertain-transaction/no-replay rules remain in force.
Complete this state-changing cohort before preparing subsequent proposals;
earlier state preimages are stale. Source/context movement requires refusal or
fresh review, never an arbitrary baseline repair.

This is not the external-exclusion `shared_bridge_predecessor_migration.apply()`
route: that API remains unchanged and must not be used for an unjournaled import
inside a maintained window. Window owner admission must continuously exclude
and drain old/new direct bridge and imported cadence readers and all competing
source/state writers before first read and through publication/restoration.
The `analytics_brain.lock` / `analytics_brain.state.lock` transition does not
establish exclusion for old loaded code. Maintained flocks and ordinary-reader
cohort seals are not hostile-root/same-UID or retained-memory guarantees.

No carrier/job/schedule or inventory-pin write is authorized. All **26** drift
inventory source-pin occurrences, installed inventory and global manifest stay
unchanged; inventory adoption is a separate later reviewed step. Default PR71,
PR119 and GLP331 generations, PR122 callback, PR126/PR131 keeper and PR130 carrier
are preserved. Disposable fixtures/CI are source proof, not independent review,
owner authority, runtime installation or permission to enter a live window.
