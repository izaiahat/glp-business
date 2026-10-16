# Ship a registered GLP job

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
