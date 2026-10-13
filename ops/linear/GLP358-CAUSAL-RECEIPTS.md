# GLP-358 causal retention (candidate source only)

The registered runner captures immutable `INPUT.json` after its existing second
IDLE guard and passes its directory to the coordinator. This does not upgrade
any historical receipt, grant dispatch authority, or change admission/closure.
The maintained coordinator prompt owns dynamic readbacks and `DECISION.json`.
Each eligibility evidence file is issue-keyed, with observed `live_owner`,
`dependencies_met`, `held`, and supporting source identities/reasons. Print each
retained readback's relative path and SHA256 in its actual terminal readback and
record that native message ID. Retain the exact issue-keyed Linear board as well.
Do not fill unknown input fields with false. An incomplete capture is not proof.

Executable read-only candidate:

```text
python3 -B scripts/auto_coordinator_receipt.py verify --first /home/ubuntu/business/reports/ops/auto-coordinator/causal/<actual-first> --second /home/ubuntu/business/reports/ops/auto-coordinator/causal/<actual-second> --closure-message <actual-native-id> --activation-message <actual-native-id>
```

Use genuine native execution/session/message IDs, not report assertions. The
first natural run must have a native action and the subsequent natural run must
reconcile that exact logical identity with unchanged PLAN and consuming inbox
bytes and zero new effects. A different issue or failed worker retry is not
idempotence. Prior dispatch reconciliation requires the genuine completed
worker RESULT readback (`DONE_READY_TO_CLOSE`) and a terminal native child.
Natural close retention still requires poller READY, actual closer stdout and
Done readback; the existing closer alone owns business-output admission.
The historical GLP-355 row/typed worker/Done and timestamped closure/resume
messages establish the prerequisite, never a new closure.

SQLite checks run only on stable in-memory copies. Nonempty WAL is refused:
use `--state-db` / `--executions-db` for owner-retained coherent native snapshots,
not manual checkpointing, immutable-mode WAL omission or synthetic records.
No runtime snapshot producer is introduced in this PR.

Tests are explicitly offline fixtures and can exercise both PASS and refusal;
they are not runtime evidence or independent acceptance. Source review, parent
adoption, genuine future natural receipts and parent-only closure remain required.
