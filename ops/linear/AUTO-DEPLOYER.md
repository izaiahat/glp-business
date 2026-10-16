# Accepted-PR auto-deployer (GLP-358)

## Boundary

Receives source acceptance, never writes a GitHub review or a Linear status.
Only `izaiahat/glp-business` and `izaiahat/glp-site-candidate`, open non-draft
PRs to main, CLEAN, no HOLD/do-not-merge label, one literal body line
`fleet-ticket:GLP-N`, and **every** exact-head check-run completed SUCCESS
(nonempty, including previous check attempts; no neutral/skipped fallback).
No branch protection is assumed. No auto repair loop or review bypass exists.
One independent review, at most one author repair, then new exact-head review;
review dispatch/repair remains the coordinator's responsibility, not this job's.

## Machine-readable independent COMMENT review

The latest submitted, non-dismissed review on this exact head must be COMMENTED,
submitted **strictly after the head committer timestamp**, and start with ACCEPT,
ACCEPT_SOURCE or APPROVE_SOURCE_ONLY. A later rejection/comment blocks reception.
Use this body contract (full 40-character head in both GitHub commit_id and Head):

```text
ACCEPT_SOURCE
Reviewer: Astra
Source-author: Sol
Independent: true
Non-author: true
Head: <40-character current head SHA>
Scope: independently reviewed source and scoped runtime installation
<findings and test evidence>
```

Reviewer and source-author are independent worker identities, NOT GitHub logins:
this fleet uses a shared GitHub account. All five fields are mandatory, unique;
reviewer and source-author must differ. This is a coordinator-submitted independent
review attestation, not proof that a different GitHub account clicked a button.
Never submit this attestation on your own implementation. Old heads, old clocks,
ordinary PR comments, GitHub APPROVED reviews, self-review and free-form unbound
acceptance cannot enter this receiver. Source-only acceptance does not authorize
any business sends, ad mutations, spend, enrollment or new cron registration.

## Execution and custody

`python3 -B scripts/auto_deployer.py --dry-run` performs read-only GitHub requests
and prints per-PR decisions. No local files/locks/clones, merges, installs,
Discord sends, fleet ingestion, or Linear writes. Normal entrypoint serializes
with a nonblocking lock inherited by child commands; existing installer custody
remains intact. Re-fetch review/head/CI/holds before CAS merge:
`gh pr merge --merge --match-head-commit <head>` (never --admin/--auto/force).
Read back merged=true, original head, merge SHA/time and direct parent ancestry.
A lost merge acknowledgement is resolved by that readback, never a second merge.

Site: receipt waits for existing hourly release job **415ec5eba194**. No second
publisher and no claim that merging proves live deployment.

Business: select only the PR's changed curated paths; exclude the global manifest,
`.github`, tests, reports, Markdown/reST and task metadata. Runtime deletion or
rename and uncurated runtime changes refuse before merging: this installer has
no scoped retirement API. Clone genuine main ancestry into Hermes scratch,
check out the exact merge commit, and require selected payload equal reviewed
head. Invoke the **existing installed** `scripts/sync_from_repo.py` with only
repeated `--only PATH`, `--checkout`, `--root`, in dry/apply/dry order. The last
must report `diffs=0 conflicts=0`. No adoption, global manifest install, global
sync, scheduler activation or executable business effects. An existing live
unrecognized file remains refused by the installer; never adopt by inference.

Before merge, retain an exclusive, fsynced no-overwrite intent in
`reports/ops/auto-deployer/.state/intents/`. Final receipts are exclusive,
fsynced files at `reports/ops/auto-deployer/<UTC>/<repo>-<pr>.json`, with head,
review, checks, merge readback, install output, and target hashes/modes. Repeat
heads are skipped. Receipt always has `live_output_proven=false`.

After uncertainty, inspect the exact merge/target paths; never replay merge,
apply or send. Unfinished intents are inspected even if PRs have disappeared
from the open list; a recovered BLOCKED_UNCERTAIN receipt stops subsequent work.
An operator/coordinator must reconcile a blocked attempt under a separate exact
recovery instruction; do not delete/overwrite history to retry. Scratch checkouts
are retained temporarily for diagnosis, not used as installed source authority.

Append a digest-bound row under the existing `reports/ops/fleet-inbox/`
`auto-deployer.jsonl`; `fleet_ingest.py` validates the receipt and applies its
unchanged coordinator-bound PLAN, genuine worker RESULT, safe proof and closer
checks. Receipt/source acceptance alone **cannot** close an issue. The existing
`linear_close.py` retains all live-output requirements and sole closure authority.
No fake completed worker or proof command is generated. Existing closure scheduling
must separately be enabled by its owner; this PR does not resume held jobs.

One ops-alerts line goes through `post_discord_message.py`; durable send intent
precedes POST, no blind resend after failure. Scheduler delivery is **local**,
preventing duplicate Discord delivery. After a crash following the final receipt,
the next run recovers only an absent digest-bound inbox row and an unclaimed
notification stage; it never repeats a merge or source install. A claimed send
without an acknowledgement remains uncertain and is never resent automatically.

## Review and activation handoff (not executed by this PR)

After independent Astra exact-head acceptance and green CI, the parent merges and
installs only this PR's runtime cohort via the existing scoped installer. Copy the
reviewed wrapper byte-for-byte into the supported Hermes script directory, compare
bytes, then register exactly one job (first inspect existing names to avoid duplicates):

```sh
install -m 700 /home/ubuntu/business/ops/hermes-cron-scripts/auto_deployer.sh /home/ubuntu/.hermes/scripts/auto_deployer.sh
cmp /home/ubuntu/business/ops/hermes-cron-scripts/auto_deployer.sh /home/ubuntu/.hermes/scripts/auto_deployer.sh
hermes cron create '5 * * * *' --name glp-auto-deployer --script /home/ubuntu/.hermes/scripts/auto_deployer.sh --no-agent --deliver local --failure-deliver local --workdir /home/ubuntu/business --script-timeout 1800
```

No registration, merge, installation, Linear closure or Discord post is performed
while building this PR. Read back the created job's no_agent/script/schedule and
its next natural execution; independently verify actual release/live output.

## Offline verification

`python3 -B -m unittest discover -s tests -p test_auto_deployer.py -v`

Fixtures cover accepted green CAS/readback, each admission refusal, stale head and
clock, empty/failing/pending/wrong-head CI, missing ticket, shared account independent
review, held labels, latest rejection, head races, repeat receipts, dry-run zero
writes, lost merge acknowledgement, crash intents, scoped dry/apply/dry0 with no
adoption, uncertain apply target inspection, and fleet digest/closure-proof gating.
GitHub Actions checks out the exact PR head, with no network used by these tests.
