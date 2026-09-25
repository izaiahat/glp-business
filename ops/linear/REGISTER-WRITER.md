# Register writer — how a seat writes the GLP-38 closeout register

Authority: audit finding 2026-09-16 (critical) — *"GLP-38's closure artifact, the 96-row
register, has no registered writer and three seats are told to edit it concurrently."*
Implements WORKER-STANDARD 6 ("One owner writes each shared file") and CLAUDE.md's
PERSISTENT PATH ONLY for `reports/ops/glp38-20260915/operator-note-closeout-register.json`.

**The register is written by exactly one program: `scripts/register_writer.py`.**
Nothing else writes it — not a seat's editor, not a `python3 -c` one-liner, not a helper
inside `reports/ops/glp38-20260915/`. That file carries GLP-38's acceptance evidence; two
seats inside one read-modify-write window silently revert a promoted row, and the only
party who can notice is the orchestrator re-reading all 96 rows by hand.

## The three commands a seat needs

```bash
cd /home/ubuntu/business

# 1. Read the row. This prints the exact value you must claim in step 2.
python3 scripts/register_writer.py show --row D51

# 2. Promote it. --expect-status is the matrix_status you just read.
python3 scripts/register_writer.py promote \
    --row D51 --seat glp-scout \
    --status MET --expect-status UNMET \
    --reason "Selected dose and its bound price render on the card at both viewports" \
    --evidence "reports/ops/glp38-20260915/scout-round3/d51-settled/observations.json:1-40" \
    --evidence "reports/ops/glp38-20260915/scout-round3/d51-settled/inputs.json:1-12"

# 3. Confirm the register is still internally consistent.
python3 scripts/register_writer.py verify
```

`promote` prints one JSON object. `"result": "ACCEPTED"` with `"exit": 0` means the write
landed and was read back from disk; the object carries `changes` (field, from, to),
`prev_sha256`, `new_sha256`, the gzipped pre-write `snapshot` path, and the refreshed
`counts`. Paste that object into your RESULT as the evidence for the row. Nothing else
counts as a promotion.

## Exit codes — what each one means for you

| exit | meaning | what you do |
|---|---|---|
| 0 | `ACCEPTED`. The write landed and read back. | Quote the JSON in your RESULT. |
| 2 | Usage/input error (unknown row id, status outside the enum, MET without `--reason`/`--evidence`, UNMET without `--remaining`, non-settable field). | Fix the command. Nothing was written. |
| 3 | `REFUSED` — your claim is stale, or the mutation is a no-op. Another seat moved that row between your read and your write. | **Re-read with `show` and decide again.** Do not re-run the same command; it will be refused again, correctly. |
| 4 | `REFUSED` — the row's `matrix_owner` is another seat. | Work your own rows. If ownership is genuinely wrong, say so in your RESULT; only `reassign` moves it. |
| 5 | Integrity failure. Either the register's tallies already disagree with its rows, or the readback failed and the write was rolled back to its snapshot. | Stop. Name the file and the printed reason in your RESULT; this is an orchestrator problem, not a retry. |

Exit 3 is the normal, healthy outcome of a race. It means the writer protected a verdict
that you were about to overwrite blind. It is not a failure of your work.

## What the writer guarantees

* **Exclusive lock.** Every decision runs inside `fcntl.flock(LOCK_EX)` on
  `operator-note-closeout-register.json.writer.guard`, so read → decide → write → readback
  is one critical section across processes. A caller that dies releases it (kernel-held),
  so the guard can never wedge the fleet. Each audit line records `lock_wait_ms`, so
  contention is observable rather than assumed.
* **Read-modify-write from disk.** The register is re-read *inside* the lock. Your stale
  copy can never be written back over another seat's landed row.
* **Claim-checked.** A mutation names exactly one `--row` and states the value you observed
  (`--expect-status` on `promote`, `--expect` on `set`, `--expect-owner` on `reassign`). If
  the live value moved, the write is refused. Two seats racing the same row therefore
  resolve to exactly one winner.
* **Diff guard.** After the mutation, every unclaimed row must be identical, the row id set
  and row count must be unchanged, and every top-level key except `rows`, `counts` and
  `matrix_summary` must be untouched — otherwise nothing is written.
* **Readback proves the write landed.** After the atomic replace the file is re-read from
  disk: the on-disk sha256 must equal the sha256 of the bytes intended, the claimed fields
  must read back as requested, and the diff guard and tally invariants must hold again. A
  failed readback restores the pre-write snapshot and exits 5.
* **No verdict is ever lost.** Statuses are limited to the enum the register already uses
  (`MET`, `UNMET`, `OUT OF ACCEPTED SCOPE`); a verdict can never be blanked; `--evidence`
  **appends** (deduped) instead of replacing; every replaced value survives as `from` in the
  audit line; and the whole pre-write file is snapshotted (gzipped, last 200 kept).
* **Audit line per mutation.** Every attempt — accepted and refused — appends one JSON line
  to `reports/ops/glp38-20260915/register-write-audit.jsonl`, chaining
  `prev_sha256 → new_sha256` so a gap or fork is detectable.

## Ownership

`matrix_owner` on the row decides who may write it.

* Empty owner → the first seat to write it takes it, and the writer stamps your seat in.
* Owner naming your seat (`glp-scout`, `glp-design`, `kael-sol`, or a prose owner that
  contains your seat token) → you may write.
* Any other owner, including prose owners like `"K10-009 production acceptance owner"` →
  exit 4. No seat promotes those unilaterally, and there is no bypass flag.

Re-attribution is an orchestrator verb and is itself compare-and-set and audited:

```bash
python3 scripts/register_writer.py reassign --row D46 --seat glp-scout --expect-owner glp-design
```

## The other verbs

```bash
# One allowlisted field, compare-and-set. Fields: remaining, matrix_reason,
# evidence_status, candidate_fidelity, current_read. id / note_verbatim / source /
# start_line / end_line are immutable here - they are the operator's own words.
python3 scripts/register_writer.py set --row D51 --seat glp-scout \
    --field remaining --value "Dose ladder still unrendered" --expect ""

# Audit trail for one row, or the tail of everything.
python3 scripts/register_writer.py audit --row D51
python3 scripts/register_writer.py audit --tail 40

# Invariants + audit chain + "was this file written outside the writer?"
python3 scripts/register_writer.py verify

# Two-process concurrency proof, run against a throwaway copy. Never touches the live file.
python3 scripts/register_writer.py selftest
```

## Things that will bite you if you skip this page

* **`--expect-status` is not ceremony.** It is the whole mechanism. Passing the value you
  *want* instead of the value you *read* defeats it, and the writer cannot tell the
  difference. Read with `show`, then copy the value across.
* **A promote must be a real transition.** Promoting a row that is already `MET` is refused
  (exit 3), because a caller that believes it needs to set MET is working from a read that
  is no longer true.
* **`counts` and `matrix_summary.counts` are derived — never hand-set them.** Both are
  `matrix_status` tallies (`counts` also carries `total`); the writer recomputes them on
  every mutation and appends the row to `matrix_summary.promoted` when it reaches MET. The
  `disposition` column is a separate historical field and feeds no tally; mirror a status
  into it only with `--also-disposition`, and only when you mean to.
* **If the tallies are already wrong, every write is refused (exit 5).** The writer will
  not paper over a pre-existing inconsistency on someone else's behalf. Report it.
* **The JSON formatting changes on the first write.** The register was produced by five
  different ad-hoc scripts and its whitespace is a patchwork; the writer emits one canonical
  form (`indent=1`). The data is unchanged and the diff guard proves it row by row. Read the
  register with a JSON parser, never with a whitespace-sensitive grep.
* **A brief that says "update the register in place" is out of date.** Point it here.

## Verification of this writer

`python3 scripts/register_writer.py selftest` copies the live register into a temp dir and
runs the real thing, in real processes:

1. two seats promoting **different** rows simultaneously — both accepted, MET count rises by
   exactly 2, the second writer's `lock_wait_ms` is non-zero (they genuinely overlapped) and
   the audit chain is unbroken (it read the first writer's bytes rather than clobbering them);
2. two writers promoting the **same** row simultaneously — exactly one accepted, one refused
   with exit 3, and only the winner's evidence is present;
3. a non-owner seat promoting an owned row — exit 4, register sha256 unchanged;
4. a hand edit made outside the writer — `verify` exits 5 and names it;
5. the live register's sha256, to show the selftest never touched it.
