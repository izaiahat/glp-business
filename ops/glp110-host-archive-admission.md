# GLP110: fresh archive admission, source-only custody

The registered user unit `host-disk-cleanup.service` executes
`/usr/bin/python3 /home/ubuntu/business/scripts/host_disk_cleanup_unit.py`,
whose single-instance owner calls `sudo -n /usr/bin/python3 -I
/home/ubuntu/business/scripts/evidence_preserving_pruner.py --full-pass --report ...`.
This repair changes that producer's `run()` bookkeeping, not the wrapper,
service, CLI scope, reference inventory, policies or effect guards.

## Authentic predecessor

Commit preceding the repair imports exactly two previously untracked maintained
files from the installed host, without changes:

- `scripts/evidence_preserving_pruner.py`: SHA256
  `ccd686864c9a589ae1e434978d0162e9639b70c2b602c5319d5e16410a1aa6d0`
- `scripts/bounded_pruner.py`: SHA256
  `ec8eab96698faf8ab835c0ab7d4c6ef9e0d85421321b1a84ce0ed68d31e5b91e`

Other runtime imports remain host-owned and are not claimed as portable source
closure. Hosted tests use explicit inert D/A/B/recovery boundary fixtures and
exercise the real C guards; no real cleanup or full-pass CLI is invoked.

## Demonstrated defect

The now-terminal run `full-20261003T231528Z` took 10047.560 seconds (worker:
10018.185 seconds), returned worker code 2, and completed with errors. It
reported 139 discovered archive candidates, zero evaluated, all six archive
roots evaluated, and no C candidate rows. The persistent path-only `C_done`
ledger causes this combination: discovery runs, then each previously held
path is skipped. Holds were memoized across invocations without fresh
reference/process/age/content admission. Reporting also counted prior terminal
paths, rather than this invocation's attempts. Full-pass A/B budgets are
per-slice, not aggregate: 39 A partial slices and 134 B partial slices precede
C. The wrapper has no worker timeout; C was reached, not killed by a deadline.

The repair preserves the legacy ledger as historical telemetry but derives
completion from a per-invocation terminal set. Every discovered candidate must
pass the unchanged current guards; changed reference inputs still remain
pending. `evaluated` counts actual admission attempts, including pending ones.
No age, minimum bytes, depth, compression, protected root or target changes.

## Effects and admission still held

61440 reclaimed bytes came from legacy safe rules; D/A/B/C each reported zero
reclaimed. This is not proof of zero writes. The D gated candidate
`glp-design-pass4-live-20260829` retains a verified manifest and text archive
created at 2026-10-03T19:25Z, before the terminal 23:15Z run. Its effect ledger
contains 1577 prepared/deleted pairs with zero attributed freed bytes (hardlinks
can unlink without freeing blocks), and was last updated at 23:22:28Z during the
terminal run; journal `deleted=[]` remains stale at the pre-gate checkpoint.
The ledger has no per-event time, so exact per-run unlink attribution is unknown.
Existing C custody
also includes 19 partial archives; their journals predate this run. They are not
orphans or deletion authority. Root free-space delta -6320721920 is unattributed,
not assigned exclusively to other writers. No archive payload contents were
opened and no staging file was changed.

Changing the C producer also changes D's implementation contract because D
hash-binds this module. Existing D dry-run approval must not be silently
repinned. Source/CI acceptance does not authorize installation, D dry receipt
replacement, cleanup execution, staging recovery, lifecycle changes or
retention-policy relaxation. Parent must obtain exact next-scope admission and
focused independent review before any changed-guard adoption/effect. GLP110
remains open through actual registered runtime acceptance; public release also
retains its separate Fable hold.
