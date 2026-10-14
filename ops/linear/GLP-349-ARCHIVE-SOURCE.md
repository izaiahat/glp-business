# GLP-349 archive source successor — source review only

## Actual lineage and bounded scope

The first commit in this PR curates authenticated K4c predecessor bytes,
not reconstructed code: `disk_retention.py` c18c9e9a… and
`host_disk_pruner.py` d05c344b…. Private K4c staged files were rehashed
against their final installed readback. Private receipts, reports, runtime
state and credentials are deliberately not published. Git retains these
predecessors; the public source contains no copied operational receipt.
The bootstrap host-pruner blob 75b0bee3… was a different `main` and was not
accepted as the installed successor.

The second commit imports the existing complete current maintained leaves:

- disk_retention: f0cdcaceaf8b7e33c14db9c87035e30da4ad7b294ac74b0bba0d7a02c790ad23
- host_disk_pruner: caf579c2e3d6db145a7c55e7d65197308f74f89ca5b1435c3ef833d140d3870e

These are byte-exact existing changes, not newly rewritten cleanup logic.
A pin covers a whole file, so accepting only extracted helper text would
not qualify the actual installed dependency. The complete delta is visible
between the two commits. Archive review does not authorize the pruner's
uninvoked cleanup main, unrelated retention actions, or any live installation.
The current host-pruner bytes also match the retained September optimization
source hash; that historical local implementation receipt is provenance,
not independent source acceptance or current operational authority.

## Archive dependency closure

`launcher -> weekly-pins.json -> weekly.py -> archive.py` is the registered
history-preserving chain. The service is also pinned. Archive implementation
and service bytes remain unchanged. The initial helper successor retained the
controller too; the independently requested empty-plan repair now changes only
its pre-effect plan gate and successful no-effect terminal handling:

- archive.py: d00b8e73b0647bc7635d818a6371df871e5eb48ff38cee8b9a2a6637d10853b2
- historical weekly.py: a091a3a4d7dbf2b963eebc61a48e8406cbaf6bac30e3c07e4be95fcef25bec24
- service: b523fe7a326b413d5c6b33cc38174ba8cc5775fc262c48a4123987dfc08ea3df

After successful plan and repeated preflight, the controller requires the pinned
v1 schema, the same absolute DB binding and an explicit ID list. Only a validated
empty list returns `NOOP_EMPTY_PLAN_NO_EFFECTS`, accepted by `main`, before ACTIVE
or `effects_started`. Missing/malformed/foreign plans refuse; prior ACTIVE still
refuses before planning. Nonempty work still uses the existing journal and actual
copy loader, SQL safety guards and verify-before-delete chain. No replay or
notification changes are included. The enforced pin map and outer launcher literal
are updated from the changed controller bytes, not from historical hashes.

At verified terminal retention, weekly invokes
`disk_retention.archive_retention(host_disk_pruner, ...)`. Its local call
closure is `pinned_json -> protected/protected_name + digest`,
`decompressed_digest`, and host-pruner
`process_inventory/path_is_active -> _reference_is_under/is_under`.
`archive_retention`, `pinned_json`, `digest`, `decompressed_digest`,
`_reference_is_under`, and `is_under` are AST-identical to K4c.
`protected` now delegates name exclusions to `protected_name`: `.db`,
`.sqlite`, `.sqlite3` suffixes except names starting `state-archive-` or
`archive.`; component prefixes `sealed-`, `tmux-`, `.org.chromium.`,
`state.db`, `chrome-profile`, `chromium-profile`, `cdp-`, `financial`; and exact
components `secrets`, `state`, `sessions`, `evidence`, `finance`, `payments`,
`browser-profiles`, `chromium`, `claude-1000`, `hermes-results`, `.git`,
`.worktrees`. `reports` is not a blanket name exclusion. Symlink/resolved-path/
Git-directory refusal remains; this wording correction broadens no behavior.

Both process helpers changed: the inventory now snapshots cwd/exe/root,
FD and maps references, strips deleted suffixes and resolves each unique
reference once. `path_is_active` consumes normalized `_refs` with an exact
path or child-path boundary and argv/environment checks; legacy rows retain
the previous live `/proc` fallback. Each owning archive guard still calls a
fresh inventory, including the pre-unlink guard after compression. No cached
inventory is reused across the compression effect. This is not a host-wide
launch lock, and existing inaccessible/racing `/proc` behavior is not waived
or represented as a new universal all-UID safety guarantee.

`host_disk_pruner` eagerly imports both `bounded_pruner` and `discord_fmt`.
Their current bytes already match maintained Git source. Both are now added
to the candidate pin map rather than being left as implicit imports. Other
pruner helpers and disk-retention extension imports are not invoked by this
archive path. The new manifest remains enforced, and the launcher's literal
PIN_HASH is advanced from its actual candidate bytes. The original pin
manifest/literal survive in predecessor history; no bypass or catch-all
hash exception is introduced.

## Focused executable proof

`python3 -B -m unittest discover -s tests -p test_glp349_archive_source.py -v`
loads real modules from the actual checkout and uses synthetic `/proc` and
non-SQLite archive fixtures. The helper contract is RED against the
predecessor (no `_refs` schema), GREEN against exact current bytes. This RED
is a schema transition witness, not a claim that K4c had a reproduced unsafe
archive defect. The current FD/maps/deleted-reference paths and legacy
fallback all cause real owning archive retention to refuse before effects;
a new inventory after removing the reference permits only the dry-run.
Missing quiet flags refuse copy/delete/rebuild-fts/vacuum before SQLite.
Candidate pin equality and launcher dispatch arguments are checked with a
mocked process boundary, and a coherent stale dependency graph refuses with
process/SQL/socket/signalling/unlink/replace effects denied. No host process
inventory or live database is exercised.

One additional owning regression exercises the actual empty-plan controller and
copy loader with synthetic successful preflight/plan boundaries and scratch-only
journals. The predecessor reproduces BLOCKED_COPY then
BLOCKED_AMBIGUOUS_PRIOR_RUN; the successor completes two independent no-effect
invocations without ACTIVE. Missing/malformed/foreign-plan and prior-ACTIVE
controls remain refusing, and nonempty copy refusal still preserves ACTIVE.
This is not genuine planner SQL or quiet-window evidence.

Independent finding 4273351595 is retained: `QUEUED_NO_POST` has no retry consumer,
so a no-post notification can remain undelivered. This confirmed delivery-liveness
gap remains with the same owner for separately authorized follow-through. No send,
retry redesign, or automatic ambiguous replay is authorized or implemented here.

## Mandatory receiving scope, separately admitted by parent

1. Obtain distinct non-author protected-guard review of this exact head and
   exact-head hosted CI/actual checkout evidence. This PR alone is not source
   acceptance, an installer, or permission to revive a retired job.
2. Parent reconciles explicit retirement of a0d5cb0609b9 and GLP-110 replacement
   ownership. Preserve all old artifacts and retirement reasons. No scheduler
   save/resume, profile mutation or unit action occurs as part of this PR.
3. Use the existing maintained guarded source/cohort procedure for authentic
   predecessors and the coherent successor: both whole dependency leaves,
   archive/controller, service, pin manifest, eager imports, outer launcher
   literal and rebuild-source manifest. Receiving must establish separate
   predecessor custody and exact immutable bytes; a generic blind repin or
   bootstrap-baseline substitution is not permitted. Confirm the exact native
   launcher mapping independently; no adoption state is synthesized here.
4. Admit genuine source/recovery/space prerequisites before any quiet-window
   request. Space feasibility requires real SQL-derived content/planned payload
   under separate authorization; the supplied stat-only conservative bound is
   not proof of actual insufficiency or feasibility. Copy gate is twice planned
   moved payload plus 2 GiB; initial gate is twice allocated SQL content plus
   2 GiB; vacuum gate is twice post-delete SQL content plus 2 GiB, remeasured
   after earlier stages. Verified recovery precedes deletion. A separately
   authorized fresh backup also needs coherent DB/WAL/SHM and its own space.
5. Only a separately admitted genuine default-home Hermes-off window can run
   the registered maintained unit. This TUI cannot satisfy it. Preserve all
   process, holder, turn/compression lease, repeated preflight, effect-journal,
   recovery, archive verification and no-blind-replay guards. No forced stops,
   fake quiet flags or lease deletion. Do not ask the operator to exit before
   source and real space prerequisites have been resolved.

Source acceptance does not establish <8 GB live DB, restored automated
history-preserving maintenance, or 24-hour eight-worker/swapquiet acceptance.
Those remain with the same end-to-end owner and parent's receiving/closure
authority. Kael's existing natural pass and SUP/cleanup/hold controls remain
unchanged.
