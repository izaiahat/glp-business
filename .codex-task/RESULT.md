# PR #109 review repair and GLP-44/73 maintenance

Implemented on `fix/pr109-review-findings` from `e00a32c062a92fb9a89ef7fcaf600f027e0426e4`, with a real merge of current `origin/main` (`d6858dd7687b3bf1d44faa3ac7683f44cf3d81f6`). A fresh authenticated GitHub branch read confirmed that base. Only the report, allowlist and manifest conflicted; allowlist/source entries from both sides are retained. No push, PR merge, installation, live scheduler change, nft invocation, service control, GA4 change or send occurred.

## Findings and acceptance evidence

| Finding | Fix | Regression evidence |
| --- | --- | --- |
| F1: package-child imports and aliased reads silently escaped inventory | Resolve absolute/relative imported children and package initializers. Unknown child imports, wildcards and callable capabilities refuse the claim of independence; an identifiable scheduled control is conservatively affected. | Both exact review cases are maintained in `test_maintenance_inventory.py`, plus relative/dotted imports and unresolved-capability cases. Inventory debugging reproduced five failures before the fix. |
| F2: expired one-shot and native legacy normalization broke restoration | Durable snapshot includes the exact expected paused control map. Supported native pause uses its atomic saver while preserving the original noncontrol record; restore performs an exact native saver CAS under native registry/fire locks, without resume/rescheduling. Adopted pins remain intact. No raw registry writer. | Both exact installed-native review cases in `test_maintenance_restoration_review.py` restore the complete original record, including absent fields. Native integrations assert separate scratch HERMES_HOME and disable provider notifications; they explicitly skip if the installed dependency is unavailable. Four F2/F3 cases reproduced RED before fixing. |
| F3: ACTIVE survived un-restorable scheduler changes | Compare all paused control fields, including presence, due cursor and pending slot, and entire journal-derived crontab bytes. Unrecognized pins also invalidate ACTIVE. Guards recheck paused custody at operation completion. | Exact cursor/CRLF oracles plus every control-field conflict, unrelated crontab bytes/final newline and unknown-pin status/apply refusals. |
| F4: advertised flocks accepted an unfenced mixed reader | Production adopters stage the complete sources/wrappers/sync-state cohort and use `ActiveWindow.switch_files`. Before any replacement, every public member and existing bytecode cache is mode 000. Native DAC denies ordinary new opens; descriptors opened before fencing authenticate the complete cohort before permissions reopen. The adoption guard refuses transactions bypassing this API and rechecks drainage. Ordinary failures restore the entire original cohort under the same fence. | Both original review scenarios are retained in `test_maintenance_admission_review.py`. The direct-write subprocess reproduction still demonstrates advisory-lock bypass, but the repaired guard refuses its success and journals uncertainty. The supported switch separately starts a real subprocess after its first rename: it is BLOCKED and never reads a mixed cohort. Tests also cover caches, short writes, removed-cache rollback, power loss, distinct exact recovery review and recovery interrupted again. |

Combined focused command, using the existing isolated runner:

```bash
PYTHONPATH=.:scripts:tests /home/ubuntu/.hermes/cache/scratch/glp44-glp73-test-venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_maintenance_inventory.py tests/test_maintenance_window.py tests/test_maintenance_adoptions.py \
  tests/test_maintenance_restoration_review.py tests/test_maintenance_admission_review.py \
  tests/test_maintenance_native_admission.py tests/test_maintenance_cohort_recovery.py tests/test_glp73_sync_rebaseline.py \
  tests/test_glp44_scoped_adoption.py tests/test_glp73_paired_adoption.py tests/test_glp44_reader_exclusion.py \
  tests/test_glp44_reporting_source_pin_pair.py tests/test_glp44_weekly_pin_adoption.py \
  tests/test_glp44_bridge_source_admission.py tests/test_glp44_bridge_fence_successor.py tests/test_glp44_safe_bridge_activation.py tests/test_sync_from_repo.py
```

Final combined result: **226 passed in 88.62s**, including the committed-manifest preflight, including all eight review cases and existing maintenance/adoption tests. Evidence: `.codex-task/focused-tests.log` and `.codex-task/focused-tests.xml`. New maintained suites are included in `scripts/run_tests.sh`. No full suite, build or browser run was attempted. Exact curated set and SHA pins verified for 466 tracked paths; The committed-manifest preflight passed against the actual committed tree using the writable local metadata; fixture repositories kept their own Git metadata. Python compilation, shell syntax and diff whitespace checks passed. Hosted CI and independent receiving acceptance remain unperformed.

## Bounded admission and its proof boundary

Inventory closes the actual affected Hermes jobs, crontab rows, timers and bridge actions, and keeps them closed until exact restoration. Opaque tasks with known controls are paused conservatively. Drain checks native live claims, per-job fire locks and the read-only execution ledger; stale claims need positive owner-death or matching terminal-execution evidence. Native external-worker payload identity roots its own descendant tree. In-process gateway tasks block by their durable task record, without treating the gateway's entire daemon tree as a scheduled task. Systemd service state and bridge sockets/threads/cgroup/action receipts retain their existing drain checks. Existing source/work flocks remain held throughout the window.

The process census names only actual adoption-file argv operands (including executing inline commands), open descriptors, mappings, affected launchers and their descendants. Gateway residency, agent sessions, shells, an interpreter basename and a repository cwd do not establish a reader. An unreadable unrelated PID such as sd-pam is ignored; an identified task/source reader with incomplete identity remains a named blocker. PID/start identity authenticates controller exclusions. An unreadable native ledger or unresolved task ownership still refuses that task.

This is a staged switch with a read fence, **not an atomic multi-file rename claim**. A directory/symlink rename also cannot pin two independent opens to one generation. The proof is continuous exclusion of admitted scheduled tasks plus a kernel read fence during the supported switch. Existing source descriptors/maps and identifiable direct readers must drain first. Stale bytecode is fenced and invalidated. Privileged controller/DAC override, symlinks, hardlinks, unpreservable ownership/group or changed preimages refuse. This does not establish isolation against hostile same-UID chmod, root launching an unregistered reader, or hidden previously retained source in memory. Those are outside bounded task admission; no whole-host idle claim is made.

The complete prepared cohort and preimages are durable before fencing. A crash leaves admission closed and public files sealed. Status refuses ACTIVE/exit and reports retained cohort recovery. Reviewed recovery authenticates custody and exact target inode/size/mtime/owner metadata, reconstructs the complete preimage with closed permissions, verifies every new descriptor, then restores modes. It never temporarily chmods a sealed file readable merely to inspect it. Recovery changes no scheduler pins or transaction outcome; those require the separate existing exact outcome review when uncertain.

## Today's read-only process evidence

**The exact host PID blocker list is UNKNOWN in this execution environment.** `/proc` and `ps` expose a private PID namespace: PID 1 is Codex, followed by only this command's shell/process. Host gateway, scheduler workers, sd-pam and other host sessions are inaccessible. Zero visible blockers is not evidence that the host is clear. The controller refuses the live default root from this known private namespace rather than accepting incomplete drainage.

Sanitized evidence is retained in `.codex-task/live-processes.json` (captured 2026-10-01T04:12:55Z). Read-only registry evidence showed 144 jobs and two disabled/paused jobs with September 9 run_claim records, `b5522c79be59` and `0993c4d2cd4e`; the records expose no PID field. They are task-ownership diagnostics, not proof of current live processes. No PID, owner death, service state or host all-clear was invented. The updated native admission algorithm uses actual claim `by` identity and the execution ledger rather than truthiness. Obtain the exact current PID list on the host using the `readers` command below. No live process was signaled or stopped.

## Runtime re-baseline

The fixed successor hash/three-path allowlist is removed. `glp73_sync_rebaseline.py --receipts` reads the anchored original snapshot, current state and a supplied JSON list of authorized receipt descriptors. It generates the complete runtime diff/current digest, verifies receipt and authority-evidence hashes, validates ordered contiguous transitions for every changed path, and checks current successor bytes (or authorized removals). Missing, extra, gapped, malformed or moved evidence/state/source refuses. Original `BEFORE`/paired-adoption `STATE_BEFORE` remain unchanged. The proposal embeds the supplied descriptors; adoption regenerates it from current files before accepting a distinct reviewer's exact proposal.

Receipt descriptor format (repeat changes/receipts as needed; do not copy old successor hashes):

```json
[{"path":"reports/actual-sync.json","sha256":"<receipt SHA256>",
  "authorization":{"path":"reports/actual-authority.json","sha256":"<authority SHA256>","pointer":"/authority"},
  "changes":[{"path":"<moved relative source path>","before_sha256":"<predecessor SHA256>",
              "after_sha256":"<successor SHA256>","after_pointer":"/actual/after/hash",
              "before_pointer":"/actual/before/hash"}]}]
```

`before_pointer` is optional for retained legacy receipts containing only successors; the anchored state and complete ordered chain still bind the predecessor. Receipt/authority pointers must resolve to retained real evidence. The independent reviewer authenticates the authority's provenance and scope; the producer does not turn arbitrary prose into permission. Every movement, including a further protected-oracle successor, needs its receipt. No current host re-baseline or approval was fabricated in this task.

## Updated receiving runbook

Execute only from the independently reviewed committed checkout, as the workspace owner, after receiving source acceptance. Use the existing Hermes Python; no installation or runtime command below was executed by this task. Keep all preexisting provider/publish/spend/send holds. The bridge's existing target-kernel nft/sudo prerequisites still require receiving-side validation.

```bash
TASK_CHECKOUT=/path/to/exact/reviewed/checkout
TASK_PYTHON=/home/ubuntu/.hermes/hermes-agent/venv/bin/python
TASK_RUN=/home/ubuntu/business/reports/ops/glp44-73-maintenance-<fresh-id>
TASK_JOURNAL="$TASK_RUN/window.json"
mkdir -m 700 "$TASK_RUN"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" readers \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes --journal "$TASK_JOURNAL" > "$TASK_RUN/readers.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" plan \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes --journal "$TASK_JOURNAL" > "$TASK_RUN/plan.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp73_sync_rebaseline.py" \
  --root /home/ubuntu/business --receipts "$TASK_RUN/authorized-sync-receipts.json" > "$TASK_RUN/rebaseline-proposal.json"
```

`readers` is ps/proc only and names direct source readers; full plan/enter also covers scheduled task owners. Require actual host visibility, complete four-surface coverage, no inventory errors, and a fully explained re-baseline. Related PID blockers drain through their owners; unrelated gateway/shell/session residency does not require shutdown. Never kill an effect to meet a deadline. The receipt list must already contain the actual authorized sync evidence. An independent reviewer creates `rebaseline-review.json` with schema `glp73-sync-rebaseline-review-v1`, decision `ACCEPT_EXPLAINED_DIFF`, actual distinct `prepared_by`/`reviewer`, and `proposal` equal to the complete generated v2 proposal.

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" enter \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes --journal "$TASK_JOURNAL" \
  --drain-seconds 300 --window-seconds 3600
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp73_paired_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" --state-rebaseline "$TASK_RUN/rebaseline-review.json" > "$TASK_RUN/glp73-preview.json"
```

Enter must return `state=active, verified=true`. The bounded detached keeper owns the full lock set. Timeout restores exact controls; a conflict retains `recovery_required`. Use fresh journals; old pre-repair journals without exact paused maps cannot prove ACTIVE. Run GLP-73 first because both adoptions change shared sync state. Independently accept its exact preview fields (everything except `applied`) with schema `glp73-paired-adoption-review-v1`, decision `ACCEPT_EXACT_BYTES`, source `209c3beea6e2045dcb81367d5339a23a225eda40`, actual reviewer, including `state_rebaseline_sha256`.

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp73_paired_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" --state-rebaseline "$TASK_RUN/rebaseline-review.json" \
  --review "$TASK_RUN/glp73-review.json" --apply > "$TASK_RUN/glp73-apply.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp44_scoped_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" > "$TASK_RUN/glp44-preview.json"
```

Require verified status after the first apply. Independently accept every field of GLP-44's freshly generated `proposal`, including original/paused job and fresh state preimage, with schema `glp44-scoped-adoption-review-v1`, decision `ACCEPT_EXACT_BYTES`, approved_source `1c459c7aff4a17063e803ba610627663d2025254`, and actual reviewer.

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp44_scoped_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" --review "$TASK_RUN/glp44-review.json" --apply > "$TASK_RUN/glp44-apply.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" exit --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
```

Before exit, verify actual source/wrapper bytes, modes, pins and unrelated records against the exact reviews/custody. Expect closed/unverified after exit, exact crontab bytes/control field presence and original one-shot due time, prior timer enablement/active state, removed maintenance ingress gate, adopted pins preserved and every original hold retained. Restoring an expired original one-shot does not promise it will execute; it gives back the original record. Installation is not live analytics/publishing acceptance.

On sealed switch interruption, keep tasks paused. Status emits `cohort_recovery`. Generate reconstruction evidence:

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" recover-cohort \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes --journal "$TASK_JOURNAL" > "$TASK_RUN/cohort-proposal.json"
```

A distinct reviewer accepts its exact proposal with schema `glp-maintenance-cohort-recovery-review-v1`, decision `RESTORE_EXACT_COHORT_PREIMAGE`, and actual distinct preparer/reviewer identities. Then reconstruct without restoring admission:

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" recover-cohort \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes --journal "$TASK_JOURNAL" \
  --cohort-review "$TASK_RUN/cohort-review.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
```

If the source transaction is uncertain, status emits the existing `outcome_proposal`. Independently accept the exact coherent before/after readback in `outcome-review.json` (schema `glp-maintenance-outcome-review-v1`, decision `ACCEPT_EXACT_OUTCOME`, distinct preparer/reviewer, exact proposal), then `exit --outcome-review "$TASK_RUN/outcome-review.json"`. Unknown inode/bytes, mixed cohorts, moved receipt, scheduler save or foreign hold refuse; never replay an uncertain saver or overwrite unknown source. Keeper death before source mutation can use the existing same-journal enter/exit recovery. Retain all journals and custody.

## Local Git custody

Despite the stated writable metadata, the sandbox binds the original per-worktree metadata and `.git` pointer read-only. Common objects/refs are writable. `.codex-task/git` is a private writable per-worktree metadata directory with the same branch HEAD, a copied index and `commondir` pointing to the existing shared Git directory. All merge/stage/commit operations use:

```bash
export GIT_DIR=/home/ubuntu/business/.worktrees/codex-ultra-pr109-fix/.codex-task/git
export GIT_WORK_TREE=/home/ubuntu/business/.worktrees/codex-ultra-pr109-fix
```

The commit updates this assigned branch in the common repository; no alternate source checkout or remote is used. Original immutable metadata is untouched, so its old index remains stale. Use these environment variables for subsequent Git status/work in this worktree until the environment restores metadata writability. Predecessor ancestry and current-main merge ancestry are verified after commit. Historical reports from both merge parents remain in Git history and local ignored copies; the conflicted task report is replaced by this explicitly requested report.

Final test plumbing uses a temporary Git launcher only for `git -C` targeting this exact worktree, routing those reads to `.codex-task/git`; independent fixture repositories use `/usr/bin/git` unchanged. This addresses the read-only stale index without changing test assertions or repository source.

The full run used committed source head `b01b274`. The final amendment changes only this report and its manifest pin; tested implementation and test blobs are identical. The committed-manifest check is also run on the final amended head, with evidence in `.codex-task/final-manifest.log` and `.codex-task/final-manifest.xml`.
