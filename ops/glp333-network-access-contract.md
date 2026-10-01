# GLP-333 API-first access contract

`affiliate_network_access.py` exposes `program_status`, `terms_commission`,
`issued_tracking_link` and `conversions_earnings`. Every call returns an
`AccessResult` carrying source, fetch time, coverage, safe receipts, and explicit
access state. Unknown/partial coverage cannot carry numeric totals. Access
support does not imply complete business terms, current approval, paid cash,
REAL classification, or attributable GLP earnings.

## Implemented source

- Existing Awin, Impact, CJ, Affiliati, Partnerize and HealthTrader clients own
  auth and provider schema. The contract reads exact program metadata and
  returned links; it never invokes applications, link generation or trackers.
- Six financial collectors: Awin, Impact, CJ, Affiliati, Katalys, Partnerize.
  CJ retains its existing bounded posting-window collector; Katalys retains its
  client and shared rate limiter. New orchestration uses existing credential
  helpers and demonstrated read routes, not another secret store.
- Awin uses contiguous <=30-day windows. Impact and Affiliati use bounded pages,
  stable counts and unique event IDs. Partnerize stops PARTIAL at a next page
  rather than guessing a cursor or following an arbitrary URL. Katalys requires
  a count that reconciles; a bare `rows: []` is not measured zero.
- Only source reported accounting fields are projected. Fleet commission and
  cash remain null; TEST/REAL and attribution remain UNKNOWN. No ledger writes.
- MaxBounty, tenant-bound Everflow and XenTraffic remain PORTAL_ONLY where no
  authorized API-only read is bound. Katalys metadata remains PORTAL_ONLY.
  Awin/Partnerize deterministic URLs do not prove an issued-link read.
- ShareASale remains a visible legacy row with canonical owner Awin; it causes
  no duplicate reads or applications.

The weekly source shell wrapper selects `--live-network-readonly`; the job and
reporting CLI forward it to the shared contract. Every roster rail receives an
observation, even if UNKNOWN. Deadline exhaustion remains an explicit gap.
Legacy `--live-cj-readonly` remains available; the two modes are mutually
exclusive. Offline mode does not read credentials or providers.

## Evidence and limits

`reports/ops/glp333-network-access-contract-20261001/RESULT.json` includes all
56 retained route buckets / 224 cells and exact set reconciliation. The roster
is dated September 8, not a fresh statement of 56 active accounts. Each cell
separates authenticated access from observation coverage. `LIVE-READS.json`
contains fresh safe receipts; predecessor observations are retained separately.
No terms or issued URL bodies, credentials, customer or click data are committed.

Fresh September 21–28 account reporting reads returned complete empty payloads
for five rails. These are bounded account row counts, not cash or GLP revenue.
Katalys remains PARTIAL; Impact's program terms fields were absent. Source
implementation and fixture success do not prove the next scheduler receipt.
The host wrapper is not installed by this PR; merge, sync/release, natural-run
acceptance and wider F01/F12 caller migration remain separate.

## Verification

Run `PYTHONPATH=scripts:. python3 -m pytest -q tests/test_network_access_contract.py
 tests/test_glp44_reporting_source_pin_pair.py tests/test_glp85_cj_reporting_identity.py
 tests/test_glp319_incomplete_period.py` on one line. Fixtures deny provider calls
and secret reads. Dedicated CI checks out and asserts the exact PR head.
Historical identity inverse hashes are preserved by undoing only the three
new wrapper substitutions before executing the old oracle.
