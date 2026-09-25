# GLP business control-plane source

Private, curated code/instructions snapshot. This is not a copy of the live business workspace or a source of private runtime data. `ops/rebuild-source-manifest.json` is the explicit file list and source-hash ledger. It includes direct live cron entry scripts, native Hermes cron wrappers (under `ops/hermes-cron-scripts/` for review, **not** automatically installed into `~/.hermes/scripts`), selected regression tests, operator instructions, and reviewed policy registries. It excludes live scheduler configuration/state, secrets, raw portal exports, private affiliate URLs, SQLite, reports/history and the site candidate tree. Separate source systems remain prerequisites for full runtime restoration.

## Changes and installation

1. Fetch `main`, create an isolated worktree and branch, edit a scoped set, update manifest hashes/count for every changed tracked file, and run `bash scripts/run_tests.sh` plus `gitleaks git --redact --no-banner .`.
2. Push a PR. Do not merge until targeted CI and appropriate independent/approval gates pass on the final head. This repo does not assert branch protection or authorized hosted Actions minutes; check billing/protection before relying on automation. A merge is not a release or spend authority.
3. From the live workspace, run `bash scripts/sync_from_repo.sh --dry-run`, then `bash scripts/sync_from_repo.sh --apply`. It fetches private `main`, refuses local edits to curated files, and copies only the allowlist. It does not restart crons or deploy a site. Native Hermes wrappers require a separate reviewed installation plan; a sync does not touch `~/.hermes/scripts`.

If the initial live workspace contains a curated file that differs from remote, sync refuses to overwrite it. Reconcile the delta into a PR or deliberately restore the reviewed bytes first. State is kept outside Git at `ops/.rebuild-sync-state.json`.
