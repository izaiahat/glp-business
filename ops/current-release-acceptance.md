# Current-release acceptance binding (F06)

The legacy bridge route remains unchanged. The current route authenticates the retained **actual** final combined Astra review by its source-reviewed SHA-256, the pinned operator designation, its pinned PREPARED.json, and the native GLP43 whole-release closure. It does not manufacture a Discord receipt or infer full-suite acceptance from GLP42's selected 580 Node / 173 browser gate.

The complete current-tree `npm test` result is separate evidence. A machine-produced proposal retains its honest classification, every failure name, complete counts, log pin and source-custody result pin. Even a clean suite requires the designated independent acceptance; failed tests are never silently dropped or reclassified as passes. Accepted names become only a name-bound publication allowance under the existing classifier, not product-quality certification, a deployment grant or a substitute for the next candidate's complete suite.

## Supported producer

After reviewed source adoption, from the workspace:

```sh
python3 systems/seo-blog-machine/scripts/accepted_failure_contract.py prepare-current \
  --pointer reports/glp-clinic-scout/seo-machine/current-content-base.json \
  --suite-stdout reports/ops/glp73-suite-to-green-20261002/SUITE.stdout.log \
  --suite-result reports/ops/glp73-suite-to-green-20261002/RESULT.json \
  --suite-exit-code 1 --minimum-tests 2321 \
  --preparer-id <actual-author-session-id> --out <new-proposal-path>
```

`prepare-current` creates only a new review input, refuses overwrite, and cannot install a binding. Its current real counts are 2,221 passed / 90 failed / 10 skipped; all 90 remain **unaccepted** until the independent reviewer explicitly decides otherwise. A reviewer may approve source but refuse that allowance. Do not ask an author to fill in acceptance to unblock the job.

The designated independent reviewer, not this producer or its author, supplies `glp-current-release-suite-acceptance-v1` containing:
- `verdict: APPROVE`, `blocking_findings: []`, `publication_allowance_only: true`;
- `proposal: {path, sha256}` for the exact generated proposal;
- `accepted_names` exactly equal to that proposal's observed `classification.new_failures` (empty for a clean suite);
- `reviewer: {id: <actual-independent-session>, model: gpt-6-astra-900k, provider: openai-codex, independent: true}`;
- `authority: {path, sha256}` for the pinned operator Astra designation.

This is an independent adjudication, not a generated test result. The reviewer must verify the retained full-suite execution/source custody, not accept merely because a JSON says `source_unchanged`. The coordinator authenticates the returned review and supplies its expected SHA-256 through the existing trusted-review-pin boundary (as with `current_approval`). A model name, self-declared role, or a hash calculated from an arbitrary file is **not** independent authority. No cryptographic reviewer signature is claimed.

```sh
python3 systems/seo-blog-machine/scripts/accepted_failure_contract.py install-current \
  --pointer reports/glp-clinic-scout/seo-machine/current-content-base.json \
  --proposal <exact-generated-proposal> --suite-acceptance <independent-review> \
  --expected-suite-acceptance-sha256 <coordinator-authenticated-review-sha256>
```

Installation recomputes the proposal, validates all retained descriptors and names, checks the current pointer, then uses the existing runtime binding installer/collision guard. It never edits the release or pointer and has no network, npm, deployment, remint, IndexNow, or schedule effects. No other release/review is admitted by this fixed current-review anchor; a different reviewed release requires its own reviewed successor.

## Live-run boundary

The registered Price Index job deploys publicly, remints the pointer and sends IndexNow notifications after candidate/suite gates. **Do not execute or enable it under source-only authority.** Source merge/adoption, authenticated full-suite allowance, coherent guarded source-pin adoption, and explicit public-release authority are separate prerequisites. The underlying job's publisher/readback/hold checks remain unchanged.
