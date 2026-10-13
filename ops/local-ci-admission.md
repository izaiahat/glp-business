# Exact-head local CI during the operator-approved Actions billing lock

Registration: `ops/local-ci-runner.json`. Authority is the installed
`ops/linear/approvals/OPERATOR-DECISION-20261013-public-repos-local-ci.md`.
This changes only CI admission, not merged-source, installed-byte, output,
independent-review, build, hold, publish, rollback, or pointer-CAS gates.

## Produce evidence

From the maintained business source, with dependencies installed in a disposable
venv, run:

```
python3 -B scripts/local_ci_runner.py \
  --worktree /home/ubuntu/business/.worktrees/<retained-exact-head> \
  --pr https://github.com/izaiahat/glp-business/pull/<number> \
  --head <full-40-character-PR-head> \
  --out /home/ubuntu/business/lanes/<lane>/local-ci-<head>
```

The site repo is also registered. Source worktrees must be clean before, during,
and after execution. Keep the receipt, command logs and exact-head worktree
available through release/closure; do not rebase or delete their retained source.
A fresh set of runner-owned output files is required; an existing lane directory
may contain RESULT/review artifacts. Failed runs write FAIL receipts and never
register admission. Receipts are external to the tested worktree.

The runner gets the PR head and changed files from GitHub. It derives the entire
applicable `pull_request` workflow run-block plan from committed source, including
workflow, job, step, exact command, environment, dependency checkout, working
directory and workflow hash. There is no CLI command override or hand-selected
step subset. Same-head checkout/setup actions are verified prerequisites, not fabricated PASS
jobs. Each job gets a fresh exact-head clone (declared fetch-depth 0 or 1), HOME,
venv with system-site-packages disabled, writable RUNNER_TEMP, workspace,
summary and PATH. TMPDIR is a separate short, job-private directory under the
host scratch root so nested native/Chrome UNIX sockets stay below path limits;
its unique path is recorded but disposable contents need not be retained. Ignored files, user packages and venvs never carry between
jobs; within-job step state is preserved. No host VIRTUAL_ENV or PYTHONPATH is
inherited. Default Python is the producer's base interpreter, recorded per job;
a setup-python declaration instead selects and observes that exact available
major/minor (or patch) interpreter. Setup-node observes and matches the declared
version before execution, or refuses. UV (when available) seeds the fresh venv
with Python downloads disabled; otherwise the selected interpreter uses venv.
No undeclared test dependencies are borrowed from the operator's environment.

Workflow → job → step working-directory and shell defaults are honored; only
bash is supported, and an effective unsupported shell refuses. Missing/escaping
cwd, unsupported setup inputs, setup after run steps, needs/artifact transfer and
GitHub file-command state (GITHUB_ENV/PATH/OUTPUT/STATE) refuse rather than being
ignored. Reserved runner environment cannot be overridden. The one supported
secondary checkout is the public, workflow-pinned `izaiahat/hermes-agent` native
fixture, cloned and revalidated separately inside each job source. Unsupported
actions, conditions, matrices, service containers and expressions refuse.
Workflow dependency installations run exactly as declared. Setup versions must
already be available; the runner does not install new host runtimes. Receipt job
evidence pins setup logs, interpreter paths/observed versions, clean per-job
sources and isolated directories; admission rechecks that evidence as well as
every command log. Retain the jobs directory beside the receipt.

A PASS receipt is registered in the existing report tree at
`reports/ops/local-ci/<repo-name>/<head>.json`. This is an exclusive, hash-bound
locator, not an alternate task queue. An existing registration is not overwritten;
inspect it before rerunning. Pin the receipt in the PR evidence comment. A receipt
for a different head, dirty source, incomplete command list, failed command,
changed runner/registration, missing decision, changed logs or missing source
refuses. No unrelated-failure waiver is implemented by this stricter consumer.

## Consume evidence

`glp_release_main.py` discovers the registered exact-head receipt automatically;
`--local-ci /absolute/path/LOCAL-CI.json` may select that same registered receipt.
The worker RESULT for `linear_close.py` may set `local_ci` to the receipt path
(or its `{path, sha256}` binding). Automatic registry discovery works there too.
The ordinary and protected-runtime typed closers revalidate the same admission;
adding a worker field alone cannot bypass them.

Successful hosted checks always take precedence and require no local input or
operator-decision file. Fallback requires exact-head completed check runs whose
non-success jobs carry GitHub's actual account-billing-lock annotation; ordinary
failures, pending checks, empty/truncated evidence and wrong-head jobs refuse.
`ci_admission.mode` (`hosted-success` or `local-ci`) is recorded in CI, release and
typed closure receipts; local mode also pins the runner, decision, receipt and
billing-lock check IDs. Closure acceptance comments identify the mode.

Provenance uses the existing trusted host/filesystem boundary: registered source
hashes, an exclusive runner index, source identity and command-log hashes. This
is not a signature against an operator able to rewrite both code and evidence.
Installed runner/admission/registration bytes must match the producing versions,
even when paths change between source worktree and installed runtime.

## Release safety

A non-author must accept this safety-oracle change before merge. REL1 does not
merge, install, close an issue, run a live release or publish. After independent
acceptance and authorized merge, install the maintained modules plus registration
through `sync_from_repo.py` with scoped dry-run/apply/dry-run custody. Then the
release owner exercises the installed carrier; the closer owner exercises the
real typed gate with readiness/output evidence. Hosted recovery is checked on
main as required by the operator decision.
