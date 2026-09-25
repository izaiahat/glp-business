> SUPERSEDED 2026-09-11 by ops/linear/WORKER-STANDARD.md — spec history, not a build order. Do not implement anything from this file that an acceptance check does not name.

# Global fleet selector contract

Controlling authority: K10 RELAY §§2,5 and L1 item8. `scripts/linear_next.py` performs only GraphQL reads. It never claims work, changes state, creates a queue, or approves an artifact. All outputs contain `read_only:true` and `work_authorization:false`.

## Existing main and bounded polling

From `/home/ubuntu/business`:

```sh
python scripts/linear_next.py
python scripts/linear_next.py lane:ads
python scripts/linear_next.py --poll-count 2 --interval 30
```

No lane: return the sole live top-level In Progress, Blocked or In Review issue. Multiple active mains fail closed, regardless of lane. No main means NO_ACTIVE, not permission to pick an arbitrary Todo. Optional exact lane selects only a child of that main. A lane never starts a new main. Blocked/review children hold instead of yielding to later convenient work. Priority orders children only, never the main plan.

Polling refreshes the API snapshot on each iteration; count1–100, interval at least30seconds. JSONL outputs are observations. It is a runnable fallback, not an installed natural schedule, event journal, wake, dispatch lock or reminder engine. Use only through the existing single orchestrator owner; do not install parallel pollers.

## Approved ordered manifest

For progression, the orchestrator supplies an externally approved ordered manifest and its independently bound SHA256:

```sh
python scripts/linear_next.py --plan /absolute/approved-plan.json --plan-sha256 APPROVED_SHA256
```

The JSON object requires:

- `schema`: literal `linear-fleet-plan-v1`.
- `team_id`: canonical UUID for GLP.
- `orchestrator_id`: canonical UUID for the operator-key actor.
- `issue_ids`: nonempty, unique, ordered canonical UUID array. Every entry must resolve to a top-level issue in the complete GLP snapshot.
- `approval_reference`: nonempty locator of the actual approval/admission evidence.

The loader verifies digest, schema and UUID grammar, including under Python optimization. It does **not** prove that a referenced approval is valid. The existing dispatcher must bind real authority before calling it. Do not manufacture an approved manifest from current priorities or populate a fake approval reference. No K10 upload or approved-order manifest was produced in item8.

It stops at the first issue not both exactly Done and PASS-verified. A Done issue without its receipt returns MISSING_PASS. Canceled/Duplicate/Backlog/deleted states never count as completion. An earlier reopened issue with later active work yields REGRESSION_HOLD. Unplanned active work, multiple mains and invalid topology fail closed. When all preceding issues qualify, an admitted first Todo can return NEXT_MAIN for the orchestrator; lane callers receive AWAIT_MAIN_ACTIVATION instead. The orchestrator alone performs the actual state transition with immediate readback.

## PASS receipt

Before closing, the orchestrator must verify actual acceptance and any required independent review, then post a Markdown comment beginning exactly `FLEET_RESULT_V1`, followed by a fenced `json` block containing:

- `issue_id`: exact issue UUID.
- `outcome`: literal `PASS`.
- `result_path`: absolute path to `RESULT.json` inside `/home/ubuntu/business/reports/`.
- `result_sha256`: SHA256 of the exact accepted file bytes.

Use the existing client comment operation with this complete multiline body. This is not the older free-text `RESULT: PASS` example. The referenced JSON must have top-level `status:"PASS"`.

The gate reads full paginated comments and issue history. The matching comment must be authored by `orchestrator_id`; its creation/edit time must fall after the latest non-Done state event and no later than the latest operator-authored Done transition. The current issue must still be Done and not trashed. An old PASS cannot discharge a regression; altered RESULT bytes, a fleet-authored close, a later comment edit, missing history and wrong identity all fail closed. Archived Done may qualify.

This proves a bound, current PASS receipt, not business truth or independent approval by itself. The named acceptance authority still owns substantive judgment. Preserve old receipts when reopening; never edit them into a new approval.

## Dependencies, errors and concurrency

Every incoming `blocks` edge for the selected main/child requires a verified PASS prerequisite. Unknown types or reversed targets are errors. Unresolved, missing or out-of-team prerequisites remain held; the selector never searches past them. `related` and `duplicate` are not blocking edges. Without an ordered manifest/operator identity, the selector cannot verify prerequisite PASS and therefore holds such dependencies.

Exit0: ACTIVE, NEXT_MAIN, SUBTASK_ACTIVE or SUBTASK_READY. Exit3: NO_ACTIVE, NO_SUBTASK or PLAN_COMPLETE. Exit4: any semantic hold. Exit2: invalid input/API/shape/auth failure. API errors are not empty queues.

Selection is a non-atomic multi-read snapshot. Recheck exact state, ownership, dependencies, scope and order immediately before the sole orchestrator's write. Item9 owns role-key refusal and hourly audit. The selector's checks are real code, but neither a second dispatcher nor an independent acceptance identity.

## Current evidence

### K10-002-A item2: local reservation primitive
`initialize_reservations(path)` explicitly creates a new owner-only SQLite ledger; it refuses existing files. `reservation_target(selection)` extracts exact root/child IDs and updatedAt values. `reserve_next(path, client, lane, plan, operation_id, owner, expected, revision)` calls the existing selector inside BEGIN IMMEDIATE, rechecks PASS/dependencies and the expected snapshot, and atomically reserves one child per lane under one root. All lanes must use the same ledger and admitted plan. Operation IDs are canonical UUIDv4; owner is a bounded opaque worker identifier, not proof of authentication.

Exact replay revalidates selection and reconciles the same persisted operation. Changed requests, stale revisions/snapshots, another root, occupied lane, plan changes and inconsistent/missing/corrupt state fail closed. SQLite synchronous FULL plus explicit initialization directory fsync provides local durability. There is no automatic expiry, release, root switch, ledger recreation, remote claim, dispatcher or business authorization. Reservations return work_authorization:false. A local transaction cannot make Linear reads/server changes atomic. The caller still owns admission authority; this library does not authenticate an approval reference or defend against a privileged filesystem writer.

No production ledger or new poller was installed in item2. Real SQLite concurrency and fresh-process restart were tested against synthetic Linear reads, including the actual PASS verifier. Regression suspension/reopen, release/advance and live sandbox acceptance remain subsequent work. Operator reorder: finish item2, implement GLP-13/K10-002-B interface first, then return here, then GLP-15. Additional pending requirement: selector output carries the issue's Effort level; dispatcher applies the corresponding lane /reasoning disposition for every task.

K10-002-A step1 repairs the native API adapter: history supplies `actor{id}`, explicitly normalized to the verifier's `actorId`; comments now request `archivedAt`, which the verifier requires. The prior normalized unit fixtures did not exercise these query-to-verifier gaps. `tests/test_linear_pass_api_shape.py` covers native-shape accepted PASS, wrong/null closer, archived receipt, wrong author and changed RESULT bytes. Accepted PASS selects the next ordered root, never a later higher-priority root. This repair is not the persisted reservation, concurrent-claim or regression-transition implementation; those K10-002-A acceptance criteria remain pending.

`PLAN/L1/ITEM8-TESTS.xml`, `ITEM8-LIVE-SELECTOR.json`, `ITEM8-LIVE-REQUESTS.json`, and `ITEM8-PARENT-WORKSPACE-READBACK.json`. Former per-lane source/tests remain in `PLAN/L1/item8-selector/preimage/`; historical item5 results do not validate the new governance.
