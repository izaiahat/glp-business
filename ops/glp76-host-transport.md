# GLP-76: existing host/bridge transport successor (source only)

This change ports the published `qlugNldijmBDs3Em` updater's argument and result
semantics into **the existing** `scripts/cron_bridge_action.py` caller. It does
not install or invoke anything. No new runner, state store, daemon or schedule.
Native R2 stays paused; the n8n article-carrier stays inactive.

## Binding and invocation boundary

The future, separately authorized entry is the existing host caller with action
`glp_seo_updater`, using the same interpreter as the installed host cron:
`/home/ubuntu/.hermes/hermes-agent/venv/bin/python`. Do not substitute
`/usr/bin/python3` for this caller: that interpreter lacks `croniter` on this host.
The bridge's **child** interpreter remains its existing `python3` resolution
(`/usr/bin/python3` in the installed acceptance); these are different processes.

Accepted CLI arguments are `--arg pt-date=<AUTHORIZED_PT_DATE>` OR the pair
`--arg review-request=<ABSOLUTE_REQUEST_PATH>` and
`--arg review-request-sha256=<EXACT_SHA256>`. The existing empty refresh-argument
case is retained, but is not authority to select a default operation. Scheduled
and due-slot invocations are refused for this successor. Review-only is effectful
advisory review, **not** a nonmutating preview; there is no preview/dry-run option.

The caller uses its unchanged `make_call` loader and authenticated loopback
bridge HTTP client, then `POST /run` with `{action: script, name:
glp_seo_updater, args: ...}` and GET-only `/job/<id>` polling. It never fires the
n8n webhook or requests an n8n callback. The installed bridge still authorizes
its bearer scope, allowlists arguments, admits work and executes registered
`[python3, scripts/glp_seo_updater_run.py]`, cwd `/home/ubuntu/business`, timeout
8400 seconds. Host polling retains that deadline plus its existing 120-second
readback margin. Neither argv nor timeout is overridden in the payload.

The bridge registry, daemon and credential/HTTP helpers are pre-existing runtime
prerequisites, not new curated copies in this small PR. The private repo is a
curated source mirror, not a standalone deployable bridge. The fixture contract
is a non-installable excerpt, not a replacement `actions.json` or authority.

## Preserved gates and result handling

- Existing action flock, pending-submission journal, payload digest, failure
  streak notification and uncertain-POST/no-replay behavior are unchanged.
- The **same updater** still owns the global lock, stable topic+route claim,
  generation/plan and review-request hash checks, quota, route collision checks,
  independent review, serialized release, live content/lastmod verification,
  publication receipt, content-base and Output Health gates. None is edited.
- The updater exits zero on domain failures. The former generic host path could
  report these as PASS. This successor requires its machine receipt and refuses
  blocked/unknown statuses, truncated output and contradictory effect flags.
  A failed postcondition retains the known pending job for GET-only readback;
  do not delete it or change arguments to circumvent reconciliation.
- Terminal replay: a finished bridge job is immutable, so re-reading it can only
  repeat its verdict. `BLOCKED_CURRENT_GENERATION_PLAN_MISSING` and
  `BLOCKED_NO_READY_PACKET` with no operation and both effect flags false are
  returned before any claim, attempt or publication; they are archived as a
  terminal refusal (rc 125, alert raised) rather than held forever, so a later
  authorized invocation of the same argument reaches the updater's own gates.
  Review-only refusals with both effect flags false are archived the same way;
  the updater replays its own persisted review receipt and never republishes.
  Every other blocked/unknown status (update failure, effectful child
  in-progress/ambiguous, adapter error, contract violation) may follow a partial
  effect and stays held for GET-only reconciliation.
- Review-only has no publication heartbeat/send. Its returned digest must match
  the request and both effect flags must be false.
- Refresh success requires PASS_REFRESHED and both true effect flags, then a
  first-party URL whose path equals the returned route and HTTP 200 before
  bookkeeping. Redirects, `/go/`, query strings and foreign origins fail closed.
  This intentionally tightens the old graph's unconstrained redirect behavior.
- The three existing no-op statuses count zero, never a new publication.
  Bookkeeping uses existing `heartbeat` (native Data Table API, not a workflow
  execution) and `post_once` (ops-alerts). Notifications use truthful updater
  status instead of the graph's stale article-carrier schedule/dry-run wording.
  Failure reporting stays on the existing failure-streak path; full machine
  failure details remain in the durable bridge receipt.

## Evidence and limits

`tests/test_glp76_host_transport.py` has one focused test for each changed
transport contract, plus execution19075 and retained auth/uncertainty regressions.
Network and subprocess creation are denied; only transport and external sink
boundaries are mocked. These fixtures prove transport behavior, **not business
success**, a live authorized invocation or publication.

Execution19075 stopped at `Manual Run` with `Execution limit reached.` before
bridge dispatch. The regression drives the actual host CLI through an isolated
POST/GET bridge boundary, preserves the 8400-second timeout and rejects any
webhook/callback fallback. The installed material-date/startup acceptance is
reused, not rerun as this transport's acceptance.

Remaining release-owner work is separate: review/merge, explicit installation,
then fresh exact continuation authority and a separately authorized registered
run with live pointer/bookkeeping acceptance. The September 3 plan has no new
current-generation authority. Neither this PR nor passing fixtures closes GLP-76.
