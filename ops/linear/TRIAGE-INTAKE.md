# Constrained Triage intake

`linear_triage.py` is the intake path for genuinely new out-of-scope ideas from authorized lanes, TUIs, children and machines. It is not task admission. Fable and Izaiah decide disposition and ordered placement; ordinary in-scope repairs remain inside the active issue.

## Input

JSON requires exactly `operation_id`, `source_id`, `source_label`, `title`, `summary`, `lane`, `class`, `out_of_scope`, `privacy_reviewed`.

- Save one canonical UUIDv4 operation ID and reuse it on every retry.
- Stable source ID: `namespace:opaque-token`, never raw email, subject, URL or private path.
- Configured source labels: `source:tui`, `source:child`, `source:cron`, `source:inbox`, `source:ops-alerts`. Exact IDs are resolved live. Parent readback: `PLAN/L1/ITEM8-SOURCE-LABEL-READBACK.json`.
- One valid lane and class. Title <=200 characters, summary <=2000. Both booleans must be literal true.
- Input values include their prefixes, e.g. `"lane":"lane:kael"` and `"class":"class:ops"`; bare `kael`/`ops` are rejected before journal access or network effects.
- An authorized writer supplies a privacy-reviewed minimized summary. This may be a lane/machine, not a mandatory new human approval. The booleans are attestations, not automatic PHI detection. Never feed raw email, credentials, health records, tracking links or private reasoning.

## Commands

The CLI and Python producers call `linear_triage.submit_idea`. Candidates are native
Linear **Triage** issues: priority 0, no parent or assignee, project Triage, existing
source/lane/class labels. They are visible but NOT admitted or executable work.
The existing `ops/linear/triage-journal.json` remains the idempotency/history ledger,
not the inbox. No REQUEST, reminder queue or new daemon is created.
Authority: `reports/ops/triage-redesign-20260914/BRIEF.md`; this replaces the
journal-only decision, not the existing active-root or promotion gates.

| Entry class (`--entry-class`) | Source label | Lane |
|---|---|---|
| tui-kael | source:tui | lane:kael |
| tui-design | source:tui | lane:design |
| tui-scout | source:tui | lane:scout |
| tui-ads | source:tui | lane:ads |
| native-child | source:child | submitting parent lane |
| background | source:cron | producer lane |
| inbox-email | source:inbox | authorized submitting lane |
| discord | source:ops-alerts | authorized submitting lane |
| orchestrator | source:tui | lane:fable |

Class is inferred from the existing source label and lane when omitted. Native
children, registered cron/timer producers, inbox writers and Discord handlers
use this same CLI/function with a minimized candidate and stable source/operation IDs.
Background IDs identify the originating finding, not each poll or reminder;
inbox/Discord IDs are opaque event IDs, never raw message text. Native email
forwarding is not a substitute for this registered intake entry.

`--purpose new-initiative` is the default. Writers classify existing in-scope
repairs as `--purpose in-scope-repair` and authorized email offers as
`--purpose authorized-email-offer`: both return `NOT_NEW_INITIATIVE` with zero
triage or REQUEST effects, even when an input carries `out_of_scope: true`.
These events retain their existing authorized handling; do not forward raw mail.

For probes, use a `TEST:` title and `--test`: this is a real Triage effect, not a mock.
A supplied `--subissue-id` is UUID
validated, not resolved through the network or turned into a REQUEST binding.
Repeat the original input/options to preserve source/operation identity and digest;
conflicting inputs refuse. The duplicate lane monitor and reminder owner are retired.

```sh
python scripts/linear_triage.py --input /absolute/minimized-intake.json --dry-run
python scripts/linear_triage.py --input /absolute/minimized-intake.json --apply
python scripts/linear_triage.py --idea 'Minimized idea' --lane scout --class ops
python scripts/linear_triage.py --blocker 'Minimized observed blocker' --lane scout --class ops
python scripts/linear_triage.py --discovery 'Minimized discovery' --lane scout --class ops
python scripts/linear_triage.py --list
python scripts/linear_triage.py --reconcile
```

JSON input requires explicit mode; the three shorthand forms apply by default.
Python producers can also use `purpose='blocker'` or `purpose='discovery'`;
the kind appears in the minimized issue description. A finding never grants itself
admission. Source/operation identity and payload digest remain locked. Publication
persists the remote UUID before sending, verifies the exact issue, and does not
blind-retry an ambiguous creation. Original local candidate payloads are retained.

## Review and promotion

Operator: native GLP Triage inbox, also project Triage
`https://linear.app/tilton-group/project/triage-3a4d88dd7388`.
Orchestrator: `--list` reads that live inbox, not a local list.
The already-enabled `hermes-default-linear-audit.timer` runs hourly. Its existing
`--notify` path includes every pending Triage ID and link, including fresh entries;
the Pacific date gives unchanged candidates a daily review reminder using the
existing notification deduplication. No second carrier is installed. The existing
shared `Triage aging >24h` view remains the aging escalation surface.

One explicit orchestrator action admits a candidate (replace GLP-n with its read-back ID):
```sh
python scripts/linear_orchestrator.py triage GLP-n admitted 'Evidence and reason' --root GLP-30 --basis operator-priority --project 'Ops & capacity' --type task
```
Allowed bases are `operator-priority`, `escaped-safety-revenue-defect`, and
`current-acceptance-dependency`. The last requires `--blocks GLP-n` naming the
actual acceptance work. The current root is resolved and validated live; do not
reuse GLP-30 after the root changes. Existing operator credentials and evidence
judgment remain required. The action binds Todo, direct parent, real project,
preserved lane, and removal of the triage label, with exact readback. A mere comment
or a candidate's label is not admission. `deferred` and `needs-input` stay Triage;
`rejected`/`duplicate` retain their explicit terminal disposition.

## Existing history

`--reconcile` reads every journal entry and current exact remote identity. The
2026-09-14 readback found all 78: 65 existing unique Linear IDs (including all four
ATTEMPTED entries) and 13 local-only candidates. Nothing was deleted, recreated,
marked stale merely by age, or silently treated as zero.
The orchestrator's `--reconcile --apply` publishes only the local-only candidates
into Triage, retaining their original source/operation IDs, digest and payload.
It preserves the 65 historical entries and native dispositions. Unverified IDs
are reported explicitly; they are never recreated. Review overlap with existing
work in Linear rather than inferring semantic duplicates from similar titles.
In the commissioning brief, live Linear mutations are reserved to the orchestrator;
the worker prints `NEEDS-ORCHESTRATOR` and does not perform this migration or promotion.

## Historical evidence and references (not current execution policy)

The following item8/item9/K10 notes are historical and do not authorize new
forwarding or a monitor restart. The full old text is
preserved at `reports/ops/system-simplification-20260914/migration/step-7/before/ops/linear/TRIAGE-INTAKE.md`.

Item9 live acceptance created GLP-137 through the actual default-fleet CLI and shared journal; its creator is verified ops user `858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7`. Role enforcement and hourly audit are implemented; see SOP.md and `PLAN/L1/ITEM9-VERIFICATION.json`. This does not claim every producer is connected; that remains the separately assigned K10-002-D work.

Parent exercised the actual CLI with live GLP metadata in dry-run; one test per acceptance check (network denied). Live issue creation and ops@ author proof belong to item9, not item8. No inbox/cron/TUI producer, reminder, notification or business runner was installed or resumed here. Item9 owns fleet-key role enforcement and the interaction SOP. See `REMINDERS.md` and `DISCORD-RELAY.md` for unshipped integration contracts.

Native email intake is enabled, with its literal authenticated UI address privately stored in `ops/linear/email-intake-address.private.json` (0600). The API unique alias is not the full rendered local part. No email was sent or blanket forwarding configured; forward only deliberately minimized authorized intake, not raw mailbox data.
