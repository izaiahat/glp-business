# GLP-320 maintained source — SOURCE ONLY

`script/root_custody` is not a second runner: the maintained paths are
`scripts/root_custody/phased.py` and `scripts/root_custody/single_unit.py`,
curated successors of the exact report-owned producer and phase-limited caller.
The first PR commit preserves both original files verbatim (producer SHA256
`3931c9d50602137d887f3a5fd3dfe0e3a14ee434eb15e627e49c9679a3e4c7f7`, caller
`9c7b77cd4e33a442d55956489ec7c8869254d48a9a705e4128fe9a1fab8c2348`).
Historical report files, policy documents, preimages, authorizations and receipts
remain host-owned immutable inputs, not repository imports or migration targets.
No installation, cutover or current effect authorization is included.

## Invocation contract

- Native `phased.py advance` and the successor caller both default to one unit.
- Only explicit caller `--readonly-units N` (2 through 8), additionally bounded
  by fresh exact-digest authorization `max_readonly_units`, opts into finite
  grouping. The caller invokes the same Driver in process so initialization,
  every admission, intent, action, commit, footer and receipt verification share
  the **original** 75-second work / 230-second total envelope.
- Every unit retains full source/auth/seven-pin/preimage/prior-state/footer/process
  census, credentials, phase-appropriate IAM/preservation/cron/health and exact
  independently connected recovery-session checks. Admission requests are not
  ordinary plan units; eight plan units does **not** mean eight total SDK reads.
- Positive-listed read units only; stop before mutations, guard creation,
  binding, rollback, waits, cleanup permission/removal/acceptance or unknown
  units. Never cross a phase. A first-slot barrier returns 2; a barrier after
  committed reads returns 0 without dispatching the boundary. Dispatch it
  separately with default one and its own authority. No new scheduler/store.
- Each unit has original native intent/started/committed/footer plus caller
  ADMITTED_INTENT/COMMITTED receipt. Durable native state/footer and caller
  receipt are read back before another unit. Any ambiguity/error stops; one
  existing bounded recovery path is used when required. After guard deletion,
  off-host recovery is required, not a claimed retained local guard.
- Work=75, per-request SDK<=min(65,remaining), diagnostics=5, recovery=140
  (including its diagnostics), footer=10, total=230. No clock reset between units.
  2700-second guard, strict >1200/>225 margins, waits<=65, observation>=600,
  heartbeat<=420, metric<=300 and clock discontinuity<=5 remain unchanged.

## Fresh authorization, not reuse of rehearsal authority

The maintained caller accepts explicit run directory, authorization path and
independently supplied SHA256s for authorization, producer and caller. No default
points to the retained run or its completed authorization. The existing native
authorization conditions remain, with additional exact `allowed_stage`,
`allowed_units`, `max_readonly_units`, `caller_sha256`, `initial_unit`,
`initial_call_number`, `initial_status`, `initial_run_sha256`,
`initial_phases_sha256`, `preimage_sha256` and `observed_recovery` bindings.
Each later phase needs its own parent-authorized checkpoint binding. Repeated
waits retain the unit but advance the receipt/call number. Receipt chaining is by
native call number. Cleanup additionally needs a separately hashed independent
cleanup authorization bound to the actual preimage and cutover; having its path
alone is not approval. Completed/failed/ambiguous runs are refused.

Before ANY future effects: independent exact-head/source review, authorized
installation of the reviewed source, a **fresh native lifecycle** rather than
renewing the expired historical guard, current protected-window exclusion,
confirmed independently authenticated off-host recovery, fresh source/pin/IAM/
health admission and a demonstrated whole-path schedule including rehearsal,
handoffs, external gaps, independent cleanup approval latency, recovery reserve
and the 75-unit post-deletion tail. The source-only operator decision grants none
of these effects. The retained apply/unit0/call96 run stays stopped and unchanged.

## Offline acceptance

`python3 -B tests/test_glp320_readonly_grouping.py` runs seven focused checks with
plain Python SDK/session/config fakes, socket/process/Live-constructor sentinels,
and isolated fixtures. It carries unchanged policy/retry/F1-F3 methods by exact
AST identity and authenticates the predecessor advance AST for trace comparison.
The old broad policy matrix is not rerun. Full chronological fixture uses actual
plan/cursor/wait/read/health/mutation closure logic but prices the unchanged
29-slot simulator bodies without executing the 315 policy expectations. Its
admission time and API costs are explicitly synthetic, not future AWS latency.
The full fresh caller is separately exercised for eight consecutive real fixture
admissions, including a changed recovery identity and post-deletion refusal.

Native unit-start/body-complete/commit/footer timestamps plus caller admission,
dispatch and verified timestamps separate admission/body/durable receipt time;
foreground monotonic start distinguishes internal units from external gaps.
Local constructor-free test duration is local overhead only. A passing synthetic
schedule is neither production throughput acceptance nor effect authority.
