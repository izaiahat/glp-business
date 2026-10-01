# GLP331 fixed in-window carrier generation

`scripts/glp331_carrier_adoption.py --generation glp331-carrier-20261001`
adds one source cohort to the maintained maintenance inventory. It reuses
`fleet_pin_release.prepare_glp331`'s immutable PR117 seven-source selection,
installed dependency authentication and literal carrier hash-span substitutions.
The old fleet release default and GLP44 original/PR119/dependency generations
remain unchanged. No sync-state output is added.

## Scope and admission

Exactly seven business paths (the literal `PATHS` declaration) and the matching
Hermes reconciliation wrapper are transaction outputs. Only the withdrawal
program and withdrawal candidate may have absent preimages. They are linked
from privately named, same-directory, authenticated sealed cohort inodes during
the maintained cohort commit; authenticated alias custody is retained until
native save/readback and publication succeed. Existing bytecode handling stays
in `maintenance_cohort`; orphan bytecode on absent sources is refused.

The generation requires an already verified active window and its keeper,
operation/adopter admission, fresh full inventory, paused controls and drain.
It freezes full PAUSED records for `f0c6a95a371c`, `3c79a0b5f7b2`, and
`b0c800751b88`. Apply holds all three sorted native fire fences and the native
jobs lock. It reads the current registry, checks exact consumer records including
field presence, and changes only `f0c6a95a371c.script_sha256`. Unrelated current
records survive; a whole-registry handoff hash is deliberately not used. The
native saver runs once, followed by exact full-list readback while sources,
bytecode and new-path aliases remain sealed. Native `updated_at` envelope
maintenance is permitted, not arbitrary job field changes.

`source_transaction` and the single `job_updates` intent precede the cohort
journal snapshot. Original control/schedule restoration remains owned by the
maintenance window. No resume, schedule recomputation, claim clearing or second
save is performed by this generation. `ActiveWindow.switch_files` and its PR122
callback body, `maintenance_cohort`, and the PR126 keeper remain unchanged.

## Preview and independent review contract

Only after separately authorized window entry and caller exclusion, invoke the
maintained CLI with `--checkout`, `--root`, `--hermes`, `--window` and
`--prepared-by`. Without `--apply`, it emits `{applied:false, proposal:...}`.
The checkout must be clean; all executing producer dependencies must match its
exact Git head. Source bytes still come from the fixed accepted PR117 commit,
not from editable caller files. The proposal binds current dependencies and
sync-state preimage, eight output bytes/modes, presence-aware preimages, three
complete consumer records, registry mode and current window journal preimage.
An older v3 enabled-record review is not this paused-window proposal.

Apply requires `--review <path> --apply`, not `--prepared-by`. The JSON contract:

- `schema`: `glp331-carrier-generation-review-v1`
- `decision`: `ACCEPT_EXACT_BYTES`
- `proposal`: the complete exact preview proposal
- `reviewer`: nonempty, different from `proposal.prepared_by`
- `reviewed_at_utc`: UTC `YYYY-MM-DDTHH:MM:SSZ`, no future time and at most 24h old
- `writer_exclusion`: nonempty evidence-backed parent-owned exclusion statement
- `evidence`: nonempty array of nonempty evidence references
- `runtime_review`: `schema=glp331-carrier-runtime-review-v1`,
  `decision=ACCEPT_EXACT_BYTES`, an independent nonempty `reviewer`, and
  `proposal_sha256` equal to SHA-256 of canonical sorted-key, two-space-indented
  UTF-8 JSON plus final newline (`maxbounty_pin_release.json_bytes(proposal)`).

This source contract does not manufacture approvals. Review must be performed
independently on exact fresh bytes. The parent still must retire or exclude
already-loaded old importers, direct nonparticipating callers, parent assignments
and queued/cloud wait-resume work. The window cannot infer those acknowledgements.
No old importer or caller is modified here.

## Custody and uncertain outcomes

Preimages/candidates and a durable generation intent live under
`reports/ops/pin-archive/glp331-window/glp331-carrier-20261001`. Intent prevents
replay even with a fresh proposal or an unreadable sealed outcome. Once commit
starts, lost saver acknowledgement, mismatched readback, second-link failure or
alias-unlink failure retains seals and journaled custody, marks the transaction
uncertain, and blocks automatic restoration/replay. Reconciliation requires a
separate exact-outcome review; this generation never retries or silently rolls
back an uncertain native save. DAC exclusion covers participating ordinary
readers, not root, hostile same-UID writers or already-loaded code.

## Verification

`tests/test_glp331_carrier_generation.py` is one focused, parameterized vertical
regression. On main it reaches the actual whole-registry-CAS failure under an
unrelated record tick. On the successor it uses the real inventory and maintained
window/cohort, synthetic service/control probes and disposable job stores. It
checks unrelated churn acceptance, scoped field-presence refusal, independent
review binding, eight sealed outputs, one repin/save, exact restoration, and four
uncertainty/no-replay boundaries. Exact-head CI also runs each outcome against
public native scheduler commit `07bf28d6446025d3b3b16c758ca4a300ed6db9ba`, in a
fresh interpreter per disposable home. No live window entry, installs, job runs,
sends or credentials are needed for these tests.
