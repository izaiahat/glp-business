# Ship a registered GLP job

## GLP334 preclaim closure and immutable refused attempts

Preparation and the immediate durable-claim boundary authenticate the bounded
executing installer closure and run the real read-only `MaintenanceWindow.plan`
with all four scheduler surfaces and existing unknown/incomplete inventory
refusals. This does not replace `enter()`'s later inventory/guard checks.
The coherent additional declaration/import members are
`glp331_carrier_adoption.py`, `glp345_a4_adoption.py` and
`glp44_safe_bridge_activation.py`; the already-required enrollment checker
`glp334_pin_closure.py` is authenticated too. This does **not** adopt or import
historical `fleet_pin_release`, `wrapper_pin_sync` or `glp73_sync_rebaseline`.
A direct maintenance `claim_attempt` caller must supply its authenticated
committed `checkout=`; missing closure/planning errors create no intent.

Legacy `started` intents remain exclusive: no deletion, reset, old proposal/apply
replay, inferred terminal success, or new approval store. The supported diagnostic
is read-only and prints to stdout, without preparation, fetching, locks, window
entry, scheduler calls or original evidence writes. The CLI disables Python
bytecode generation before imports, so a cold invocation needs no caller `-B`
or bytecode environment setting:

```sh
python3 scripts/glp_ship.py aadf6677c7f0 --merge-sha ORIGINAL_SOURCE \
  --inspect-attempt --proposal ORIGINAL_READY_JSON --checkout SOURCE_REPOSITORY
```

Without independently reviewed refusal evidence it returns
`UNRESOLVED_NO_REVIEWED_BOUNDARY_EVIDENCE`. Optional `--refusal-evidence` and
`--refusal-review` are diagnostic inputs, not enrollment approvals. Evidence
schema `glp-ship-pre-window-refusal-v1` binds the raw original attempt SHA256,
canonical proposal SHA256, source, root, Hermes root, original absolute
`window_journal`, recorder (`recorded_by`) and `traceback_sha256`; it requires
`stage=MaintenanceWindow.plan`, `exception=FileNotFoundError`, one of the two
proven missing import/declaration paths (`missing_path`), and literal-false
`snapshot_started`, `pause_started`, `adoption_started`. Review schema
`glp-ship-refusal-review-v1` binds source/attempt/proposal/evidence SHA256,
`decision=ACCEPT_EXACT_PRE_WINDOW_REFUSAL` and an identified reviewer distinct
from recorder and preparer. Current source provenance, every proposal preimage,
both full target records and absence of the reviewed original window/receipt
must still agree. Ambiguous, post-window, drifted or misbound inputs refuse.

Even a consistent reviewed diagnosis returns `state=started`,
`transition_performed=false`, `replay_permitted=false`: the old intent did not
record an authenticated contemporaneous boundary or bind the original custom
output path. Absent journals and current equality do not establish that history.
For the retained `151594547d7a` / proposal `55038b429b93` attempt, the raw
`started` evidence remains immutable; no retrospective transition is supported.
Independent Astra review and separate exact receiving authority remain necessary
for any future baseline change, followed by a **new** proposal and independent
review, never the original reviewed proposal reused against changed baselines.

## Drain census and receive-only ingress

The maintained drain window remains 300 seconds. It now retains `drain` with
`started_at`, `timeout_seconds`, `last_observed_at` and the last actual `blockers`
census in its existing journal. Changed blocker sets are saved during drain;
the final timeout save preserves its last census through ordinary restoration.
Only the existing bounded reader identities/reasons/paths are recorded, not
process command lines, prompts or environments. No holder is signalled and no
deadline or drain/adoption gate is weakened.

The exact receive-only `glp_host_ingress.py serve` exemption must pin the current
reviewed source. The predecessor `0989cd700e18` became stale when committed
`5fbe89026cc1792251a3614edbaec1820af06ee2` changed ingress custody. Its successor
is `2d5aa63b6bf9416d94e74928c7a21ff587cb2900e485351430aeeff0ca544b7d`.
The serve path still only accepts/reads durable intents; action imports, bridge
calls and drains remain outside serve. Exact argv, one thread, no children,
no affected source references, no native owner and readable identity checks
remain mandatory. The regression authenticates real committed server bytes
against the production digest rather than substituting a matching fake digest.

For GLP-334's `9fa8d72d` refusal, the retained window records this ingress source
as `2d5aa63b6bf9`. PID 4045047/start_ticks 605349141 belongs to
`glp-host-ingress.service`, started 2026-10-09T01:03:39Z with no restarts and the
exact serve argv. Read-only installed census reports it as `affected_task_tree`
via `glp-host-custody.timer`'s drain trace. The stale pin therefore provides a
persistent refusal, not a holder that a longer timeout would clear. The sole
native execution overlapping the window, fleet-inbox job `bbc82551321b` run
`4512fc313854452eb6720c9a10c707ce`, failed normally at 00:29:31.762622Z,
before even the earliest possible 300-second deadline (00:31:33.872494Z).
It cannot explain the final
timeout. The old journal did not persist its busy list: this attribution proves
a persistent blocker, not an exhaustive historical census of every transient
host process or bridge thread.

A clean restored `drain_timeout` still leaves an exclusive `started` intent.
There is **no supported new attempt identifier, same-source retry or same
accepted-proposal reapply** in this CLI. Changing output or preparer does not
change the intent key. Keep both historical intents and their reviews intact.
This source repair requires Astra source review, separately authorized merge
and finite installer receiving, then a new merged-source proposal and genuine
independent exact-proposal review before any separately admitted apply. The old
review cannot authorize changed installer/proposal bytes. `--inspect-attempt`
remains read-only and reports `replay_permitted=false`; it is not recovery.

## Registered shipping

Run from a clean checkout of the merged commit, as the workspace owner:

```sh
python3 scripts/glp_ship.py GLP333 --merge-sha <full-merge-sha> --apply
```

The command fetches canonical `main`, verifies the exact merged PR and current
successful CI on its PR head, and materializes the merge commit. SHA256 pins come
from regular Git blobs in the registered cohort. Modified, unknown, symlinked,
or non-main source is refused. The executing installer must also equal that
commit. No force flag, arbitrary path list, or caller-selected review tier exists.

| Job | Runtime | Tier | Install window |
| --- | --- | --- | --- |
| `GLP44` | Registered hourly analytics bridge command | B | Existing maintenance window |
| `GLP333` | Legacy base weekly reporting wrapper | B | Existing maintenance window |
| `aadf6677c7f0` / `2ca359464bfa` | Exact paired J7 + retained consumer enrollment; no execution | A | Existing maintenance window |
| `GLP73` / either F12/F13 native ID | Paired retention wrappers | A | Existing maintenance window |
| `F01` / `58202b06bdc0` | Discovery wrapper | A | No window; existing consumer locks and owner exclusion |

GLP44 and GLP333 share the registered reporting/collector source closure.
Retention always installs the paired F12/F13 co-pins. No publishing, money,
conversion, or Sheet-writer job has a Tier B registration. Unregistered jobs
refuse; registering another cohort requires a reviewed source change.

For Tier A, prepare without `--apply`. This writes `READY-FOR-REVIEW.json` beneath
`reports/ops/glp-ship/<job>/<sha>/` (or `--output`). One independent reviewer
inspects its proposal, including exact live preimages and planned schedule
disposition, and supplies this receipt:

```json
{
  "schema": "glp-ship-review-v1",
  "decision": "ACCEPT_EXACT_PROPOSAL",
  "prepared_by": "identity from the proposal",
  "reviewer": "independent reviewer identity",
  "proposal_sha256": "hash from READY-FOR-REVIEW.json"
}
```

```sh
python3 scripts/glp_ship.py GLP73 --merge-sha <sha> --apply --review review.json
```

Identity comparison strips whitespace and ignores case. The receipt is an
operator-authenticated attestation, not a digital signature; a JSON name alone
does not establish the reviewer's real identity. The default preparer is the OS
user; `--prepared-by` records the actual responsible preparer. Changed preimages
or a changed proposal refuse before installation.

F01 also retains its **separate owner-exclusion authority**. Its review envelope
must contain `adoption_authority` with `owner`, `writer_exclusion` describing the
actual exclusion, and the same `proposal_sha256`. This is not a second independent
review and does not waive the three consumer locks. The fixed retained migration
predecessor and later Git-known baselines remain distinct admission paths.

Installation reuses the adopters' preview, custody, native hash CAS, coherent
switch, uncertainty seals, and restoration paths. Every historical named
generation retains its original pins and review requirements. Postapply checks
require exact source bytes, baselines, and registered wrapper hashes; a
conflict-free dry run alone is insufficient.

Derived reporting publications address custody by the complete exact ship
proposal, adopter publication plan and optional review, not by a null Tier B
review. Sequential legitimate source generations retain separate preimage
receipts; historical review-addressed receipt paths and collision refusals stay
unchanged.

The installer continuously holds the maintained workspace directory-inode source
writer lock through preview, custody, source/state switching, native save, exact
readback and window restoration. Nested preflights join only with an opaque live
same-process/thread/root lease; ordinary importers still refuse a busy root. The
lease expires when its descriptor closes and is not a force/bypass option.
Previously loaded importers and uncooperative writers still need the existing
owner exclusion; this cooperative lock does not authenticate reviewer identity
or replace F01's separate owner attestation. The source lock is released before
registered runtime admission; native fire fences span restoration and the run.

The maintenance window holds the same consumer locks needed by the registered
jobs. This command therefore restores/closes it before the registered smoke run,
within the same invocation. It never runs through a held fence. Failed or
uncertain runs are not retried; the selected schedule is paused with a reason.
A durable attempt record prevents repeating the same job/merge SHA, including
with a different output directory; reconcile an existing attempt before proceeding.
Native paired schedules are enabled only after both runs succeed. A collector
failure retains an exact single-entry crontab pause journal; unrelated controls
are restored by the original window. Ambiguous adoption commits retain the
existing seals and require reconciliation; the tool never forces restoration.

`RESULT.json` records the proposal hash, installed source, run exit code and
output path. A timeout or unknown launch outcome records a null exit code.
The native runner preserves its sanitization and timeout cleanup; successful
stderr is not included in its formatted output. The hourly registered command
retains its own log redirection, so its shared log must not be mistaken for an
isolated per-run transcript. Fixture tests prove the mechanics, not live output.

This tool's PR is itself Tier A. Merge only after independent exact-head review.

## GLP334 exact enrollment exception: no runtime admission

Either literal ID `aadf6677c7f0` or `2ca359464bfa` selects the same two-target,
Tier A, enrollment-only contract, including uppercase/mixed-case literal forms:
identity is lowercased **before** tier dispatch, never through the legacy alias.
No GLP334 alias, arbitrary ID, path or hash is accepted. The intentionally named
`GLP333` legacy base-wrapper route retains its original contract.
The wrapper/native predecessor bytes are authenticated against immutable
`ebd64f331d3ae825f1fc10de19d260321688accb` plus the authenticated IDs,
J7/consumer script identities, workdir, no-agent mode, timeouts and schedules.
The entire current record is then review-bound and CAS checked, not reconstructed.

The finite union declared in `ops/glp/reporting-v2/glp334-receiving-union.json`
is **14 PR554 owned leaves + 11 PR550 owned leaves - 8 overlaps = 17**. Both
accepted heads and merge identities remain independently authenticated. The
eight shared leaves use PR550's accepted source; the six F12-only leaves retain
PR554 bytes. Every selected blob/mode/type is bound to current combined merge
`064a9c433c6f473d746aa62aac2a01fbfbcaa388`, not arbitrary later main. The maintained
`receiving_cohort(checkout)` derives/dedupes the authentic owned lists, verifies
each source identity and refuses any extra/missing leaf. Analytics and financial
behavior are unmodified. Literal GLP44 remains Done, not reopened or reclosed.

All17 must **already** be coherently received under separate authority, matching
genuine sync baselines and the importer-preserved destination modes. The base
manifest's authenticated 0600 mode is retained, not normalized to Git 0644.
Enrollment does not install/heal source or create baselines. Projection checks
all retained dependencies. The new merged installer is authenticated separately
from the immutable accepted union payload; missing/drifted custody refuses.
This coherent receiving must precede any separate analytics, measured census or
fresh producer run. No analytics-only or F12-only partial installation is a
substitute. Existing no-replay and retained-consumer holds remain unchanged.

After separate source review, merge, receiving authorization and normal custody,
prepare via the existing `glp_ship.py <literal-ID> --merge-sha <installer-merge>`
without `--apply`. Independent Astra must review the fresh exact live proposal
before separately authorized `--apply --review <receipt>`. The existing window
must drain both jobs and all affected readers; source/cleanup/fire/jobs locks,
same-thread supported editor, full-record CAS, custody and uncertainty seals
remain mandatory. Only these two external wrappers and native hash fields
change; schedules are restored unchanged. The route returns zero runs and
`ENROLLMENT_ONLY_NO_EXECUTION`. Existing durable attempts and predecessor checks
prevent replay. Provider reads, cursors, money, sends, fleet repinning and any
producer/consumer execution remain separately authorized work.

Enrollment acquires the **existing** `runstate/host-disk-cleanup.lock` via the
producer's nonblocking exclusive flock, before native fire/source/window/jobs
locks. Contention refuses without dispatch, waiting or releasing another lease.
The ship caller holds it continuously through both wrapper replacements, native
save/readback, exact source verification and window restoration/readback, then
releases on success or exception. Nested adopter calls join only with an opaque
live same-process/thread/home owner from `cleanup_session`; raw descriptors,
expired owners and other threads refuse. A direct adopter acquires the singleton
itself for its effects; a caller doing its own later restoration must wrap the
whole lifecycle in `cleanup_session` and pass that owner as `_cleanup_owner`.
A merely externally held lock is not an authenticated inherited lease and fails
closed rather than deadlocking. No new approval store, global maintenance-lock
expansion or hostile-writer namespace guarantee is introduced.

Portable CI uses genuine Git predecessor/successor blobs and nonsecret native
metadata. A separate opt-in `GLP334_RETAINED_FIXTURE_ROOT` offline test can copy
real retained dependencies into a disposable post-receiving tree; it never
reads installed job records or writes the source/profile fixture root. It is
skipped when private retained inputs are unavailable, not passed synthetically.
With `GLP334_EFFECT_DENIAL=1`, tests deny network sockets and every subprocess
except a fixed read-only Git command set. The full local rehearsal additionally
runs under bubblewrap: host filesystem read-only, network namespace isolated,
and only the disposable scratch directory writable. Source projection reaches
17 selected paths / zero drifts before the copied offline enrollment; neither
check is live receiving or permission for a subsequent job run.

### Reviewed no-effect drain-refusal supersession (GLP334 only)

This is **not** a reset, replay of the original intent, waiver of acceptance, or
permission to run a producer. It is a separate metadata transition, followed (only
under separate apply authority) by one separately claimed fresh attempt. Source
review/merge/receiving and current exact target custody remain prerequisites.

`--supersede-drain-refusal` requires `--proposal <original-READY>` and the original
`--review <ACCEPT_EXACT_PROPOSAL>`, plus `--refusal-evidence`, `--refusal-review`,
`--attempt-id <new-id>` and a previously nonexistent `--output <fresh-directory>`.
It must not be combined with `--apply` or `--inspect-attempt`. In these recovery
modes **`--merge-sha` identifies the current, merged, CI-proven installer**, not a
replacement proposal. The saved proposal still names its original source/merge
proof; that source, the finite enrollment payload, every accepted preimage and
both complete target native records are independently revalidated. No proposal
is regenerated, preparer changed or new acceptance synthesized.

The exact evidence schema is `glp-ship-drain-refusal-v1`, with fields:
- `source`, `root`, `hermes`, `attempt_sha256`, `proposal_sha256`;
- `window_journal` (original absolute location), `window_sha256` (raw bytes);
- `recorded_by`, `apply_exited: true`, `exit_code: 2`;
- `stage: MaintenanceWindow.enter`, `exception: drain_timeout`;
- `adoption_started: false`, `producer_started: false`, `effects_uncertain: false`.

The exact reconciliation review schema is
`glp-ship-drain-supersession-review-v1`, with `decision:
ACCEPT_EXACT_DRAIN_SUPERSESSION`, a `reviewer` distinct from recorder/preparer,
`evidence_sha256` (canonical `json_sha`), `proposal_sha256`, `fresh_attempt_id`
and `fresh_output`. These are **operator-authenticated review attestations**, not
cryptographic proof against a hostile local writer. The reviewer must authenticate
original argv/output attribution, process exit and the historical pre-adoption
boundary; a reconstructed JSON document or equality now is not historical proof.
Missing/extra evidence fields refuse rather than silently narrowing the review.

The original intent must be exclusively `started` for exactly this proposal.
The original journal must be closed with `error: drain_timeout`, no live
keeper/controller, no remaining locks, a complete nonempty unique affected-entry
restoration list, and no adoption, transaction, outcome, recovery or unknown
fields. Every row must retain intent/pause/restore-intent/restored proof. Current
native controls/configuration/pins and crontab/systemd/bridge restoration are
read back without any saver. Existing result/enrollment receipts, a second
unbound default-output window, symlink/hardlink ambiguity, unknown process data,
missing restoration or any target drift refuse. The old pre-window attempt with
no restoration journal **cannot** take this transition; its historical bytes and
review remain untouched. The more recent clean refusal is eligible only if all
proof and current-target gates still pass—an old clean snapshot does not excuse
subsequent `ops/.rebuild-sync-state.json` or manifest drift.

Under the existing source and original-window operation locks, one exclusive
fsynced `<job>-<source>.supersession.json` records effective `state: superseded`,
the original intent path/raw digest, exact evidence/reviews, proposal digest,
and **one** fresh ID/intent/output reservation. The original intent bytes are not
changed or deleted. A second transition, different ID, existing claim, or partial
record refuses. The sidecar itself is the supersession readback; the legacy
`--inspect-attempt` continues to inspect original historical intent state and
never grants replay authority.

A separately authorized `--apply --attempt-id <same-id> --proposal <same-READY>
--review <same-acceptance> --output <same-fresh-directory>` uses the current
installer merge SHA. It rechecks the exact sidecar, original bytes, reviewed
restoration, source/payload, acceptance, preimages and complete native records,
then runs the original bounded full preclaim plan immediately before one
exclusive `.retry-<id>.json` started claim. It calls the **unchanged** enrollment
apply path: cleanup singleton, full maintenance drain, source/fire/native locks,
window verification, full-record CAS, custody, restoration and zero-run policy.
No automatic retry exists. Failure/uncertainty after claim remains a claimed
fresh attempt; neither changing output nor choosing another ID permits reuse.
