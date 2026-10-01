Implemented locally on `feat/glp44-glp73-maintenance-window`, based on PR #101 / `0e474f8f1bd691b9b9f372429fca2fee048e7f6d`. This is source implementation and offline verification, not live adoption acceptance.

**Changes**

`maintenance_inventory.discover()` enumerates Hermes jobs, user crontab rows, defined and transient user timers, and the n8n bridge listener with its actual action commands. It derives affected paths from the adoption declarations and traces Python imports, shell children, literal subprocesses, hash readers, and source writers. Opaque code behind an identifiable control is conservatively affected, so it is stopped as a whole; unavailable surfaces, malformed/sensitive commands, ambiguous controls and incomplete coverage refuse admission. Systemd lifecycle hooks and environment-file references are included without opening environment files or requesting environment contents.

`scripts/maintenance_window.py` implements `plan`, `enter`, `status`, and `exit`. Durable, fsynced preimages and mutation intents precede every pause. A detached, bounded keeper holds the existing source, analytics, article, reporting, weekly-price, and retention flocks across commands. Verification binds the complete required lock set to live PID/start/boot identity, descriptors, inodes and kernel EX locks. Crontab rows are commented and exactly restored, including ordering/line endings. Timers retain their enablement; only active timers are stopped and restored. Hermes pause/resume uses the installed supported `cronjob` tool; resume's native atomic saver restores original scheduling field presence, pending slot and due time in that same save. No raw `jobs.json` writes.

The n8n listener uses the existing external nft SYN gate from `glp44_safe_bridge_activation.py`, with exact listener/socket/thread/cgroup and durable-job drainage. It does not call credential-bearing GET/health routes, source installation, or restart. The controller runs as the business owner; only nft invokes `sudo -n`. It refuses any preexisting or unreadable gate. Timeout restores entries without killing in-flight work. A keeper crash invalidates ACTIVE status; the same journal can be resumed or restored. Pause reasons and cron comments point to retained recovery custody. Ordinary keeper verification errors attempt restoration; uncertain source, scheduler, or adoption outcomes retain an explicit `recovery_required` journal.

Both adoptions require a verified window for apply. GLP-44 independently reviews original scheduled configuration and the exact paused record. Source pins, preimages, modes, reviewed candidates, preview/apply CAS, custody, and existing rollback remain. File and pin write intents are journaled; an uncertain scheduler save is never blindly replayed or reversed. Completed transactions advance verified baselines, so a later adoption cannot admit earlier predecessor bytes. Restoration preserves successfully adopted pins and original schedule/hold state. The adoption operation lock prevents expiry/exit from restoring jobs during a write.

**GLP-73 state mismatch explained**

The retained exact snapshot `reports/ops/glp44-scoped-live-adoption-20260929/read-only-preimages/05.bytes` hashes to `20a6396a78a4cb4cf82e473b0b3805b01ed279cd2e6526c2032caa10814e7436`. The read-only current state hashes to `bfc81ed021e5c7da93498eb450c5f71797919de0b37554ed34915e30e012cb56`. Schema, all other fields, and every other baseline are unchanged. The full semantic diff contains exactly:

| Baseline path | Old hash prefix | New hash prefix | Retained authorized sync evidence |
| --- | --- | --- | --- |
| `ops/protected-oracles.json` | `ee12c01f` | `9f7d68ee` | `reports/ops/seat-continuation-sol61-20260930/RESULT.json` |
| `scripts/seat_continuation.py` | `f00211d4` | `f23afdbf` | Same two-path scoped sync receipt |
| `scripts/affiliate_sheet_batch_writer.py` | `dc713c15` | `e5a05f3d` | `reports/ops/cleanup-p0-drift-repair-20261001/INSTALL-WRITER.json` and `RESULT.json` |

`scripts/glp73_sync_rebaseline.py` verifies the exact old snapshot, full old/new state digests, three changed-path successor bytes, and pinned receipt digests. Its output records the complete diff. A distinct independent reviewer must accept that exact proposal through `--state-rebaseline`; the original `STATE_BEFORE` constant remains unchanged. Any further movement or unexplained field/path refuses. Run GLP-73 before GLP-44 because both update the shared sync-state file.

**Checks**

Focused offline checks currently pass: inventory, lifecycle, adoption refusal/re-baseline, native scheduler saves, exact preimages and rollback. Lifecycle tests use fake schedulers and disposable real keeper processes, including SIGKILL, crash-resume, expiry, drain timeout, exact restoration, concurrent enter, configuration conflicts, every required kernel lock, operation exclusion, partial writes and uncertain outcomes. Focused command: `PYTHONPATH=.:scripts:tests /home/ubuntu/.hermes/cache/scratch/glp44-glp73-test-venv/bin/python -m pytest -q -p no:cacheprovider tests/test_maintenance_inventory.py tests/test_maintenance_window.py tests/test_maintenance_adoptions.py tests/test_glp44_scoped_adoption.py tests/test_glp73_paired_adoption.py` returned **84 passed in 23.13s**. The final related exact-head check, including the new CRLF probe regressions, returned **126 passed in 25.51s** (exit 0). It additionally ran sync-state/manifest, reader-exclusion, reporting-pair, weekly-pin and bridge-admission/fence suites. `py_compile` and `git diff --check` passed; exact manifest verification checked all **432** tracked paths and SHA pins.

Read-only host re-baseline verification returned the exact three-path proposal above. Read-only host inventory saw 144 Hermes entries, all conservatively affected; it refused with `coverage={"hermes":true}` because this sandbox denies user crontab and systemd bus access. No live preview, enter/exit, adoption/apply, scheduler pause/resume, installation, service restart, GA4 Admin change, test event, send, push or merge was performed.

**Parent runbook (execute only after source review and host prerequisites)**

Use the workspace owner (`ubuntu`), the exact reviewed committed checkout, and the existing Hermes Python. Do not run the controller as root. Parent must validate the existing nft gate on the target kernel and have noninteractive privilege for only its nft commands. Park other source writers and finish/stop other user interpreters, shells, and agent sessions through their owners: the process census deliberately treats those as possible readers even when argv/cwd lacks the repository. An ordinary interactive shell or persistent gateway can therefore block drainage. Use a dedicated process whose shell is replaced with `exec` for enter. Do not kill an effect to meet the deadline. These host prerequisites are unverified here.

1. Set the concrete paths in a parent-owned session and create a new custody directory. Never reuse a closed journal for a new window.

```bash
TASK_CHECKOUT=/home/ubuntu/business/.worktrees/codex-ultra-glp44-73/.codex-task/committed-checkout
TASK_PYTHON=/home/ubuntu/.hermes/hermes-agent/venv/bin/python
TASK_RUN=/home/ubuntu/business/reports/ops/glp44-glp73-maintenance-20261001
TASK_JOURNAL="$TASK_RUN/window.json"
mkdir -m 700 "$TASK_RUN"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" plan \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --journal "$TASK_JOURNAL" > "$TASK_RUN/plan.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp73_sync_rebaseline.py" \
  --root /home/ubuntu/business > "$TASK_RUN/rebaseline-proposal.json"
```

Expected: plan exit 0 with complete four-surface coverage and no errors; re-baseline exit 0, old `20a6396a…`, new `bfc81ed0…`, exactly the three rows above. On `REFUSED`, stop before enter. Have an independent reviewer create `rebaseline-review.json` containing `{schema:"glp73-sync-rebaseline-review-v1", decision:"ACCEPT_EXPLAINED_DIFF", prepared_by:<actual preparer>, reviewer:<distinct reviewer>, proposal:<exact rebaseline-proposal.json object>}`. This lane does not supply an approval.

2. From a dedicated execution process, replace its shell with the maintained enter command (use the literal paths if session variables are unavailable):

```bash
exec /home/ubuntu/.hermes/hermes-agent/venv/bin/python \
  /home/ubuntu/business/.worktrees/codex-ultra-glp44-73/.codex-task/committed-checkout/scripts/maintenance_window.py enter \
  --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --journal /home/ubuntu/business/reports/ops/glp44-glp73-maintenance-20261001/window.json \
  --drain-seconds 300 --window-seconds 3600
```

Expected: exit 0, `state="active"`, `verified=true`, detached live keeper. This pauses the conservative scheduler cohort, gates n8n, drains work, and only then takes locks. On drain timeout, expected restoration is `state="closed"`; inspect `status`. On a restore conflict, expect `recovery_required` and reconcile before any adoption. Enter's output is not adoption approval.

3. Verify status, then preview GLP-73 inside the window.

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp73_paired_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" --state-rebaseline "$TASK_RUN/rebaseline-review.json" \
  > "$TASK_RUN/glp73-preview.json"
```

Expected: status verified true; preview exit 0 and `applied=false`. Independent reviewer creates `glp73-review.json` with schema `glp73-paired-adoption-review-v1`, decision `ACCEPT_EXACT_BYTES`, source `209c3beea6e2045dcb81367d5339a23a225eda40`, their real reviewer identity, and every preview field except `applied` exactly. Include `state_rebaseline_sha256`; preserve all original paused/retired holds.

4. Apply once, verify under the same window, then preview GLP-44 against its freshly changed shared-state preimage.

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

Expected: GLP-73 `applied=true`, exact candidate source/wrapper hashes and hash-only job changes, followed by verified true. Its predecessor preview is not a post-apply verifier. Independently accept the exact `proposal` in GLP-44 preview using schema `glp44-scoped-adoption-review-v1`, decision `ACCEPT_EXACT_BYTES`, approved_source `1c459c7aff4a17063e803ba610627663d2025254`, actual reviewer identity, and every proposal field, including `job_before`, `job_window_before`, and the fresh `state_before_sha256`.

5. Apply GLP-44 once. Verify bytes, pins, modes, selected state baselines and unrelated scheduler records against custody/reviews before exit.

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/glp44_scoped_adoption.py" \
  --checkout "$TASK_CHECKOUT" --root /home/ubuntu/business --hermes /home/ubuntu/.hermes \
  --window "$TASK_JOURNAL" --review "$TASK_RUN/glp44-review.json" --apply \
  > "$TASK_RUN/glp44-apply.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
```

Expected: `applied=true`, then verified true. Both context exits authenticate every staged file digest and exact hash-only scheduler record and commit those baselines to the journal. Independent receiving review still checks actual modes and unaffected records. Do not send events or change GA4 Admin settings for verification.

6. Restore admission and exact prior schedules, keeping adopted pins and all preexisting holds:

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" exit --journal "$TASK_JOURNAL"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
```

Expected: `state="closed"`, `verified=false`, no live lock owner; cron rows restored byte-for-byte, prior timer active/enablement states retained, Hermes original due/pending/paused fields restored, the maintenance nft table absent. GLP-73's three originally paused jobs remain paused. Restore other owner-held sessions only after receiving acceptance. Read natural analytics/article receipts separately; installation does not establish live GA4 coverage or release/publish authority.

**Rollback / crash commands**

Before an apply, or after an apply exception that fully restores preimages, run the same `exit` command above. Existing adoptions restore owned source/wrapper/state bytes only while the scheduler preimage remains unchanged. On keeper crash, `status` must return exit 2 with verified false/recovery required; repeat `enter` with the same journal to resume an incomplete, unexpired entry, or run `exit` to restore. Closed/expired windows require a new journal for another attempt.

On an ambiguous landed save, do not repeat either adoption. `status` emits an `outcome_proposal` with exact journal/source/job hashes and a coherent `side="before"` or `"after"` if identifiable. Independent reviewer accepts that exact readback in `outcome-review.json` using schema `glp-maintenance-outcome-review-v1`, decision `ACCEPT_EXACT_OUTCOME`, actual distinct preparer/reviewer identities, and `proposal` equal to the emitted object. Then:

```bash
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" exit \
  --journal "$TASK_JOURNAL" --outcome-review "$TASK_RUN/outcome-review.json"
"$TASK_PYTHON" "$TASK_CHECKOUT/scripts/maintenance_window.py" status --journal "$TASK_JOURNAL"
```

This reacquires locks, authenticates unchanged inventory/owned pauses/drain and the exact complete source/job cohort, records the reviewed landed or rolled-back outcome, and restores schedules. A mixed cohort (`side=null`), changed review, foreign hold, or unknown bytes cannot be accepted this way. Retain the journal and `reports/ops/pin-archive/glp44|glp73/<review-digest>` custody; the adoption owner must reconcile the specific conflict before restoration. There is no universal safe overwrite command for unknown bytes. Expiry/SIGTERM attempts normal restoration; SIGKILL/reboot requires explicit resume/exit from the durable journal.

**Unverified**

Live crontab/systemd/n8n coverage, target-kernel nft behavior and sudo policy, normal host interpreter drainage, existing Hermes gateway tool compatibility beyond native offline fixtures, live preview/apply, real scheduler restoration, production source adoption, natural receiving/publishing results and remote CI. The read-only plan currently refuses in this sandbox. The parent owns review, push and any runtime execution.

**Local commit custody**

The assigned worktree links to `/home/ubuntu/business/.claude-cloud-checkouts/glp-business/.git/worktrees/codex-ultra-glp44-73`; the sandbox makes that metadata read-only (`index.lock: Read-only file system`). No permission escalation was attempted. The same branch and complete modified source tree are committed in the writable local checkout `.codex-task/committed-checkout`. Its Git objects share only read-only predecessor storage. The original worktree source changes are retained, and its original Git HEAD remains the base. A standalone `.codex-task/maintenance-window.bundle` contains the branch delta for parent import; neither checkout is pushed. Use the committed checkout path above for producers requiring committed HEAD.

**Broader check result and parent import**

The authoritative committed-checkout `scripts/run_tests.sh` attempt stopped at an unchanged browser fixture: `tests/test_gpt6pro_relay_binding.py::NativeComposerTests::test_captured_initial_placeholder_uses_same_exact_preflight_reader`, **1 failed, 197 passed in 12.53s**. The Node fixture returned `read ECONNRESET` / Node.js v22.22.2. No relay or browser-fixture source changed in this branch; broader acceptance remains unverified. An earlier broader attempt used the original read-only-metadata worktree and was interrupted while stalled; it is not acceptance evidence. The focused/related exact-head checks above use the writable committed checkout and are green.

The parent can verify and inspect the standalone bundle before choosing its review branch; these are local Git reads/imports and do not push or merge:

```bash
git -C /path/to/parent-review-checkout bundle verify \
  /home/ubuntu/business/.worktrees/codex-ultra-glp44-73/.codex-task/maintenance-window.bundle
git -C /path/to/parent-review-checkout fetch \
  /home/ubuntu/business/.worktrees/codex-ultra-glp44-73/.codex-task/maintenance-window.bundle \
  refs/heads/feat/glp44-glp73-maintenance-window:refs/remotes/maintenance-local/glp44-glp73-maintenance-window
git -C /path/to/parent-review-checkout show --stat refs/remotes/maintenance-local/glp44-glp73-maintenance-window
```

Use the commit named by that bundle ref and the final handoff; the report is tracked in that same commit. Remote CI and runtime execution remain with the parent.
