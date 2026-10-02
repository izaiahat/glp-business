# GLP345 / F08: fixed PR161 monitor–A4 publication

Skills: curated-private-control-repos, host-ops-cron-systemd, healthcare-microsoft-ads.

`scripts/glp345_a4_adoption.py` publishes exactly four files as one maintained
DAC cohort: PR161 monitor, active A4 JSON pin map, **actual live** Q25 wrapper,
and the monitor's selected sync baseline. The commit callback updates only
`ea5bad730e53.script_sha256` via the native saver, under native fire/registry
locks. All target fields, unrelated records and unselected baselines survive.
No fleet scan/repin, campaign call, send, job fire, scheduler enable/disable or
source-only partial adoption is implemented.

The two predecessor snapshots here are genuine live bytes, not deployment
candidates. The older curated `ops/hermes-cron-scripts/glp_weekly_ads_analytics.py`
is incompatible with the current report-local caller: it drops result_contract
and A3 brief pins and names a different controller/map. This producer must not
substitute that wrapper or bypass any live dependency check.

## Parent install handover (separate from source acceptance)

1. Consume independent exact-head source acceptance. Obtain the serialized install
   turn; exclude/drain old importers and direct retained callers. Use the existing
   maintenance-window producer **from this accepted checkout**, including its
   updated static inventory. Never reuse an older loaded inventory/window that
   cannot include this cohort. Keep article-carrier and provider holds intact.
2. Preview with `python3 scripts/glp345_a4_adoption.py --checkout "$PWD"
   --prepared-by OWNER --window /absolute/active-journal.json`. A no-window preview
   is read-only diagnostic only. It cannot satisfy the apply review contract.
3. Independent review receipt: `schema=glp345-a4-review-v1`,
   `decision=ACCEPT_EXACT_BYTES`, exact `proposal`, non-author `reviewer`, fresh
   `reviewed_at_utc` (UTC `%Y-%m-%dT%H:%M:%SZ`, <=24h), `writer_exclusion`, nonempty
   `evidence`, and `runtime_review` with non-author `reviewer`, same decision and
   `proposal_sha256=sha256(json.dumps(proposal, indent=2, sort_keys=True)+'\n')`.
   The strings describe actual independent evidence, never synthetic acceptance.
   Shared sync-state movement or selected record/input changes require a fresh
   proposal/review; unrelated registry churn alone does not invalidate target CAS.
4. Apply once with the same command plus `--review /absolute/review.json --apply`
   in a fresh native interpreter. On uncertainty retain seals/custody and reconcile;
   do not delete `cutover.json`, replay the saver, or roll back the monitor alone.
5. Use maintained window exit/restoration; read back the full weekly record and
   exact installed files. An installed pin chain is not a completed weekly run.
   Only after parent handover may the lane run the registered monitor once.

## Existing weekly blockers are intentionally retained

The real weekly caller imports `result_contract.read_result(A3, brief_path=...)`
and verifies five map entries. At source preparation three pins already drift:
`result_contract.py`, the A3 brief, and `ops/glp/seo_data_rails.py`. The producer
binds their current bytes/modes into the reviewed proposal and reports
`weekly_residual_drift`; it does not authorize changed pins or remove checks.
These **PR161-generation** residuals are retained rather than repinned there.
The explicit reviewed PR115-after-PR165 continuation below resolves them coherently.
An off-slot report-only job emits SILENT before collection;
that is not registered weekly output proof. Weekly window reservation, ambiguity
no-replay, scheduler-only delivery, null unknown spend and all spend holds remain.

## Focused proof

`python3 -m pytest -q tests/test_glp345_a4_adoption.py`

The portable tests use authentic predecessor/PR161 bytes and a real maintained
window/cohort, with labeled isolated prerequisite bodies and scheduler fixture.
To exercise the installed native saver, select EACH parametrized switch case in
its own fresh interpreter with
`GLP345_NATIVE_CRON_SOURCE=/home/ubuntu/.hermes/hermes-agent`; the fixture sets
HERMES_HOME to its disposable root before importing cron. No live scheduler,
job execution, bridge or provider call is exercised by those tests.

## GLP341 continuation: reviewed PR115 report reader after installed PR165

The explicit `--generation glp341-weekly-pr115-after-pr165-20261002` route
uses this same producer/window/native/cohort chain, not a new adapter. It binds:

- Genuine retained reader snapshot `weekly.predecessor.py` (d51b9e…), and
  unchanged reviewed PR115 reader (05bda1…). Current RESULT closeout validation
  and the A3 authoring brief are **not report inputs** after that reviewed fix.
  Their live newer bytes are untouched; the exact frozen historical A3 receipt
  replaces those obsolete edges, not current spend/serving authority.
- Actual installed map/carrier reconstructed byte-for-byte from accepted PR165
  (9f74f3… / 3a702e…). The monitor pin stays PR161 (7bb04b…): no reinstall.
- Existing maintained PostHog module equals reviewed PR115/current Git bytes
  (e82495…). Only its obsolete map edge changes. Other map entries stay identical.
- Outputs: reader, map, actual carrier, and **only** paired reader/map selected
  sync baselines. Every consumed prerequisite is bound to the proposal, residual
  dependency drift refuses. Full native target CAS still changes just its hash;
  all scheduling, no-agent, claims and notification/reservation controls survive.

After required independent exact-head review and explicit parent single-writer
handover (F09 owns now; F07 next), use the accepted checkout for the existing
maintenance-window producer/inventory. Do not import the map/reader separately.
Control source import, if needed, is scoped `sync_from_repo.py --checkout "$PWD"
--root /home/ubuntu/business --only scripts/glp345_a4_adoption.py
--only scripts/maintenance_inventory.py --only ops/registered-producers.json
--only ops/glp345-a4/weekly.predecessor.py`: dry-run → apply → dry-run diffs=0.
No global manifest, unselected runtime source, monitor or rails replacement.

Inside that actual window, preview:

```sh
python3 scripts/glp345_a4_adoption.py --checkout "$PWD" \
  --generation glp341-weekly-pr115-after-pr165-20261002 \
  --prepared-by OWNER --window /absolute/active-journal.json
```

Use the unchanged independent proposal/runtime review schema above. Apply once
with the identical arguments plus `--review /absolute/independent-review.json
--apply`; use maintained restoration and read back all exact outputs/baselines
and the complete restored native record. A read-only no-window preview does not
grant installation. Never synthesize independent acceptance or retry a cutover.

After separate run authorization, use only the registered report entry and retain
its real output. Off-slot SILENT is honest guard verification, not acceptance.
GLP341 remains open until the **genuine Monday 2026-10-05 09:30 PT** report branch
reads exact adopted bytes, scoped platform inputs and output with zero campaign
mutation. No schedule/time override, fixture substitution, replay, monitor success
or inferred all-platform zero can satisfy that requirement. Parked ads remain held.

Focused checks: `python3 -m pytest -q tests/test_glp341_weekly_adoption.py
 tests/test_glp345_a4_adoption.py` plus the existing PR115 compatibility regression.
