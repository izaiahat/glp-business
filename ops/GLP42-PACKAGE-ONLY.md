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

## Package PR #8 without deploying

After reviewing this control-plane checkout, from `/home/ubuntu/business`:

```sh
python3 .worktrees/glp42-package-only/scripts/glp_deploy_glp34_authority_v1.py \
  --package-only \
  --site-head 2927c1ea184d2ed23aa2dd448c8fc0d04df8c37d \
  --public-root /home/ubuntu/business/reports/ops/glp42-pr8-seal-20260926/sealed-r3/public \
  --expected-tree-sha256 7de7172c5423c9cdea489db876a38b465fa23d6f54d35f06d5ae1f78048f0f3d \
  --rollback-deployment-id ca0251a2-dd4e-469c-be3b-79ee8805c5f3 \
  --package-dir /home/ubuntu/business/reports/ops/glp42-package-only-20260926/pr8-package-next
```

The output parent must already exist; output must not exist or overlap input.
Three authenticated GETs confirm project canonical production/main success,
the exact rollback deployment detail, and canonical identity after copying.
No redirect is allowed. The package-only transport has no write method and
never instantiates the live client, runs a subprocess, takes the weekly lock,
loads historical authority, or writes a production pointer.

The public digest uses the site's `ci-release-gate.mjs::treeFiles` contract:
recursive JS filename sort and SHA-256 of compact JSON `{file,sha256}` rows.
It is **not** the historical carrier's size-bearing `file_closure` digest.
Source and copied public trees are verified against the explicit expected hash;
a manifest is emitted only on success, binding head/root/hash/current rollback
and the complete file inventory. Byte changes, symlinks, special files,
current rollback drift, and existing output refuse.

This is a **public-byte custody package, not release approval**. The site head
is explicitly supplied and bound, not independently inferred from public bytes.
There are no fabricated build attestations, authority documents, or signatures.
No Functions build, external acceptance, install, deploy, rollback, merge,
or live-mode execution is authorized by a package. Existing `--verify-only`
and `--live` retain all historical gates, including their historical blockers.

## Focused regressions

```sh
python3 -m unittest discover -s tests -p test_glp42_package_only.py -v
```

The suite intercepts actual HTTP transport, fails on non-GET/body/subprocess/
write-capable-client/pointer/legacy-gate access, tests byte and rollback
negatives, compares every legacy AST node against the exact predecessor, and
exercises legacy dispatch with terminal gates stubbed (never actual live mode).
The dedicated CI job checks out and asserts the exact PR head.
