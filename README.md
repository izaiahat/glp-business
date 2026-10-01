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
