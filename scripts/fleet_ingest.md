# Fleet ingestion file contract

`fleet_ingest.py` is a no-model reader. It never applies Linear closure or redispatches.
The coordinator consumes `reports/ops/fleet-inbox/GLP-n/LANE/INGEST.json`.
Lane names append a stable identity digest so two completions never overwrite each other.
Immutable revisions live in each lane's `history/`; `LAST-RUN.json` is the poll receipt.
One flock serializes readers/writers; identical source, command and closer bytes do not rerun checks.
Changes to the proof policy or installed closer automatically retry. `--retry` explicitly repeats read-only checks.

## Terminal sources

* Native: RESULT with a dead positive PID, or a runtime-owned completed delegation manifest
  naming its exact RESULT. A live PID always wins over a terminal claim. Partial worker results
  are ingested as REDISPATCH, not reported complete.
* Codex: finished RUN.json with exit_code and RESPONSE.md. Missing/failed response is REDISPATCH.
* Pro: complete route queue row plus VERIFIED JOB.json receipt. All manifest, source reply and
  outbox file bytes are rehashed locally. No browser polling, recovery sends or CONSUMED writes.
* Dot: nonempty REPLY.md beside CONFIG.json with explicit `issue` (or `issue_id`) and `dot`.
  Optional result_path/worker_result points to a genuine machine RESULT; never synthesize one from prose.
* GitHub: enumerate exact fleet-ticket:GLP-n labels (labels are not wildcard-searchable), then
  read each repo's labeled PRs. Closed/merged completions are ingested; failed/absent exact-head CI is REDISPATCH.
* Cron: latest terminal receipt per explicitly configured registered identity. No issue inference
  from message bodies. The coordinator supplies cron_bindings from verified scheduler registration.

## Trusted proof policy

Default input: reports/ops/fable-plan-2-20261009/PLAN.json `issues[].proof_cmd`.
Coordinator overrides: reports/ops/fleet-inbox/CONFIG.json, not worker output. Example shape:

```json
{"issues":{"GLP-110":{"proof_cmd":["stat","-c","%s","/actual/backup/path"],"worker_result":"reports/ops/relaunch-20261009/glp110-pr287/RESULT.json"}},"cron_bindings":{"verified-job-id":{"issue":"GLP-44","lane":"analytics","worker_result":"reports/ops/actual/RESULT.json"}}}
```

Commands run as argv, never shell/eval. stat/test/sha256sum, read-only gh views/checks,
stdlib unittest and nontracking publisher guide/sitemap/robots curl GET/HEAD are permitted.
Prose, placeholders, shell operators and effectful commands fail closed with exact failure text.
The poller then invokes installed `linear_close.py ISSUE genuine-worker-RESULT --check-only`
as LINEAR_ROLE=fleet-ingest. This must be a truly no-Linear-write API; older versions lack
that flag and return 2. Both checks must return 0 and the source must be valid for READY_TO_CLOSE.
A Pro advisory alone cannot satisfy closure. An explicit coordinator policy may join the advisory
with an independently completed worker RESULT, whose issue and byte hash are checked.

## Registration

Install merged source with scoped sync_from_repo dry-run/apply/dry-run. Install the maintained
ops/hermes-cron-scripts/fleet_ingest_cron.sh as ~/.hermes/scripts/fleet_ingest_cron.sh.
Register once with `hermes cron create '*/5 * * * *' --name fleet-output-ingestion --no-agent
--script fleet_ingest_cron.sh --script-timeout 260 --workdir /home/ubuntu/business
--deliver discord:1487300132054765629`. Declaration is house-style validated; success is silent,
wrapper failure emits a fixed nonsecret alert. Manual acceptance uses `hermes cron run JOB_ID`,
not direct polling. Inspect durable cron execution and LAST-RUN.json. Only the coordinator
may run `linear_close.py ISSUE ROW.worker_result --apply` after consuming READY_TO_CLOSE;
the poller never closes advisory, held, protected or unaccepted issues.
