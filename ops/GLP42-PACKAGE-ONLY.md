# GLP-42 package-only carrier

This extends the registered `scripts/generate_glp34_deploy_carrier.py` producer,
not an ad hoc packer. It embeds maintained `scripts/glp34_package_only.inc.py`
into `scripts/glp_deploy_glp34_authority_v1.py`. Do not edit generated output.
The first PR commit preserves the exact installed predecessor and its pinned
build6 source/test lineage. No runtime installation is part of this PR.

## Generate in isolation

From this checkout, with a **new absolute output directory**:

```sh
python3 scripts/generate_glp34_deploy_carrier.py --prepare --emit-dir /absolute/new/output
```

This authenticates the curated build6 source/test by their original SHA-256s,
then emits the carrier and historical test without executing either. The
installed generator's default root-custody and twin checks remain in place.
Copy the produced carrier into the PR checkout only; installation is separate.
Tests regenerate and byte-compare both outputs.

## Package the historical PR #8 fixture without deploying

After reviewing this control-plane checkout, from `/home/ubuntu/business`:

```sh
python3 .worktrees/glp42-package-only/scripts/glp_deploy_glp34_authority_v1.py \
  --package-only \
  --site-head 2927c1ea184d2ed23aa2dd448c8fc0d04df8c37d \
  --public-root /home/ubuntu/business/reports/ops/glp42-pr8-seal-20260926/sealed-r3/public \
  --expected-tree-sha256 7de7172c5423c9cdea489db876a38b465fa23d6f54d35f06d5ae1f78048f0f3d \
  --build-receipt /home/ubuntu/business/reports/ops/glp42-pr8-seal-20260926/gate-complete-head/gate.json \
  --expected-build-receipt-sha256 5bef33e20846da0d1f40389a66b41aafa639797c158bcfaf18eb1fd764f4f961 \
  --rollback-deployment-id ca0251a2-dd4e-469c-be3b-79ee8805c5f3 \
  --package-dir /home/ubuntu/business/reports/ops/glp42-package-only-fix-20260926/pr8-package-next
```

This historical head is a binding/rehash fixture, not acceptance of a current
site successor. The output parent must exist; output must not exist or overlap
input. Never recalculate the receipt pin just to make changed evidence pass:
obtain it from the reviewed build/gate evidence. The example pin is recorded in
`reports/ops/glp42-pr8-seal-20260926/RESULT.json` on the host.

### Required source-to-output binding

The carrier reads the existing native `glp-d1-release-result-v1` gate receipt
without following symlinks, verifies its exact SHA-256, requires `status=PASS`,
and compares its `head` to `--site-head`. Both `output_before` and `output_after`
must name `--expected-tree-sha256` and contain the complete matching file rows.
Missing, tampered, wrong-head, wrong-tree, wrong-schema or failed receipts refuse
before remote reads or creation of the output. The receipt is checked again
before the completion manifest, so drift during copying also refuses.

A fixed-repository `gh api --hostname github.com --method GET
repos/izaiahat/glp-site-candidate/commits/<40-hex-head>` read must then return that
exact SHA. An unknown commit, unavailable CLI, auth/network failure, malformed
response or mismatched SHA refuses; there is no offline or syntax-only fallback.
This exact GET is the **only** package subprocess. No shell or deploy subprocess
is used. The invocation needs an already authenticated `gh`; it never signs in,
changes credentials or writes GitHub state.

Three authenticated Cloudflare GETs confirm project canonical production/main
success, the exact rollback deployment detail, and canonical identity after
copying. No redirect is allowed by the Cloudflare transport. It has no write
method and never instantiates the live client, takes the weekly lock, loads
historical authority, or writes a production pointer.

The public digest uses the site's `ci-release-gate.mjs::treeFiles` contract:
recursive JS filename sort and SHA-256 of compact JSON `{file,sha256}` rows.
It is **not** the historical carrier's size-bearing `file_closure` digest.
Source and copied public trees are verified against the explicit expected hash.
After the final rollback read, receipt check and manifest write, the carrier
re-hashes the actual output tree against the manifest hash and complete inventory
as its final validation. Any mismatch removes the whole package (including its
manifest), returns BLOCK and emits no success stdout. A successful manifest binds
head/repository/receipt path and hash/public root/tree/current rollback and the
complete file inventory.
`provider_reads=3` counts Cloudflare GETs; `github_reads=1` counts the commit GET.
Byte changes, symlinks, special files, rollback drift and existing output refuse.

This is a **receipt-bound public-byte package, not release approval**. The receipt
pin is an explicit trusted input, not a newly created attestation or signature;
GitHub confirms commit existence, while the pinned native gate receipt supplies
the head-to-output binding. The carrier does not rebuild or independently certify
the gate's entire test suite. No Functions build, external acceptance, install,
deploy, rollback, merge, or live-mode execution is authorized by a package.
Existing `--verify-only` and `--live` retain all historical gates and blockers.

## Focused regressions

```sh
python3 -m unittest discover -s tests -p test_glp42_package_only.py -v
```

The suite intercepts actual Cloudflare HTTP transport and the exact fixed GitHub
GET argv, failing on non-GET/body/arbitrary subprocess/write-capable client/
pointer/legacy-gate access. It tests zero and real-but-different heads, missing
and tampered receipts, receipt schema/head/both-tree inventories, receipt drift,
commit read failures, byte and rollback negatives. It compares every legacy AST
node against the exact predecessor and exercises legacy dispatch with terminal
gates stubbed (never actual live mode). The dedicated CI job checks out and
asserts the exact PR head. Tests use explicitly synthetic receipts and provider
responses, not fabricated production attestations.
