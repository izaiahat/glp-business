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
| `GLP333` / `aadf6677c7f0` | Weekly reporting wrapper | B | Existing maintenance window |
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
