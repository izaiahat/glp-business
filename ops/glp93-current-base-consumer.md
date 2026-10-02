# GLP93 authenticated current-base consumption (source only)

The wire-backlog worker historically accepted only direct children of
`site-system/flagship-v2/releases`. The genuine current pointer selects the
registered GLP43 published prepared release under `reports/ops`; directory shape
alone rejected it before a plan existed.

The consumer now preserves the old direct-child contract and adds only a
canonical-current prepared-base branch. It validates the shared pointer schema,
binds the current pointer's regular-file bytes/identity, and calls the existing
`glp_price_index_weekly._validate_base_evidence` before and after reading the
registry. That helper recomputes complete release/public/deploy closures and
consumes the retained native preparation/package/build/publication chain. No
provider API, publisher main, preparation, build or publication entry is called.
Raw outside-root paths cannot become source overrides. The first batch source
registry read now retains the full current pointer; intermediate direct-child
stage trees retain their existing path-only contract.

## Verification boundaries

`tests/test_glp93_current_base_consumer.py` executes the actual consumer and
first/intermediate batch expressions, plus unchanged weekly evidence helpers.
Only the omitted eager builder and the native publisher fixture boundary are
isolated for portable offline CI. Synthetic fixtures are not real approval.
A separate pre-window host replay authenticated the actual retained current
release and native publisher chain, observed original planner refusal, and
reached the next private queue-read boundary after overlaying the candidate
consumer. It deliberately did not create a complete plan or fake queue data.
The report binds that replay to its actual candidate bytes; final exact-head
portable CI is separate. No production-root planner/build/import is authorized
during the parent's GLP337 window.

## Later guarded adoption inputs — not an installer

Use the existing source synchronizer and existing pin-adoption owner, after
independent review, explicit reader/writer exclusion and merge. This PR does
not merge, install, repin, restart, change a current pointer or close an issue.

- Selected maintained source: `scripts/glp_affiliate_wire_backlog_worker.py`.
  Installed/sync predecessor SHA256:
  `59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05`.
  Candidate SHA256:
  `83ee8b438ef7b11850a8357b0938753db414267470a00220054eb10c87bb1a9b`.
  Existing selected sync baseline was present and matched; do not pass a
  reviewed-baseline override or force option. Re-read fresh preimages at admission.
- The scoped synchronizer's existing `--only` selection admits this path; first
  use its preview, then one separately authorized apply, then zero-diff preview.
  No preview/apply was launched against the production workspace in this task.
- The reconciliation wrapper has a **pre-existing stale** `b2_pins` literal for
  this worker: `1f6988537a04f9b0813240a7c637cdd1bba840f61155b6af2717dbe016417c6a`.
  A later coherent adoption must move that exact member to the candidate hash
  through its registered reviewed pin-release path, and reconcile the actual
  scheduled wrapper's `script_sha256` if the installed wrapper bytes change.
  Source-only sync alone does not establish scheduled-consumer closure. No
  runtime registration was read, edited or saved here; no broad fleet release
  is authorized by this source PR.
- Existing call chain: reconciliation wrapper ->
  `glp_affiliate_wire_backlog_daily.py` -> worker `build_plan` ->
  `_registry_by_id`. Plan verification and current-predecessor consumption also
  use the same helper; the first batch registry expression retains its pointer.
  No daily caller, wrapper, publisher, weekly source or GLP73 preparation file
  was changed. Existing override sealing/live-head gates stay separate.
- Reuse unchanged weekly validator and shared schema, authenticated against the
  reviewed checkout. Their installed observed SHA256s were respectively
  `14f9777dc7fea07cc3226d7a89c2666f9b5293d5592ed484e9efa5f48dca7aed` and
  `e1c2c57c90f08503742a1d0ad7256c10f89473e39f1feb7c717764e749bbc479`.
- Exact later Sesame row remains Impact advertiser/program `13292`, Sheet row
  `740`; existing MaxBounty `27300` packet is not interchangeable. No issuer
  URLs, terms, provider rows or held-link rules changed. A full planner rerun
  requires separately admitted private queue reads; preparation/build requires
  the parent's existing heavy/build admission, not a reader exemption here.

Source correctness and CI do not authorize live adoption or provider activation.
