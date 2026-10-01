#!/usr/bin/env node
import { access, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { pathToFileURL } from "node:url";

// Exact protected current-base code/selection contract, not a universal report
// path or permission to adopt a candidate. The registered worker chooses and
// authenticates its predecessor, then passes a detached descendant via --release.
// Refuse obsolete GLP34 code before importing it; never repair it in this reader.
const GO_REFRESH_CONTRACT = Object.freeze({
  "src/affiliate-link-map.mjs": "3bb817c4a29504af087e1c06a4e8a25adeac0696e8cee371158530f659b86c45",
  "src/citation-attribution.mjs": "5c10bc66371d34aab8428490d565f0315239e1be77e045fb55a153502b98ebc7",
  "src/cost-calculator.mjs": "f3f8ad84786d5dc64aad8536b2bca158316801aef28b37f0c8916209e9033ad1",
  "src/go-price-basis.mjs": "97c8b49881d5b844e4c46ad2b6e853a0ee52be06f119dac85309af7e30c90b0e",
  "src/go-promotion-contract.mjs": "f33979da215d0387b08a3b3d4e7e6e1dece4ef1425a2ada8018f0cab7cadb94b",
  "src/go-redirector-authority.mjs": "451b71b940334fdc5bdcb5d764976f5bea0b39e254a0e8b80a4076af15e5a800",
  "src/go-runtime-manifest.mjs": "427903be905dc402b4fb8e32390b76a4ed0ac6120e8622cf177b386be580c6a4",
  "src/go-runtime-selection.mjs": "ea509fe3c1a2a261c66ad948e92a378892e046ea8e7ac43ff51656bdc48a492e",
  "src/medication-options.mjs": "0308ca9fcbf2dbe6bec76ed0cd906c0ee7d8e066d14f7e74b31d85db57420fcb",
  "src/price-state.mjs": "a3bd6f053dea1e4f367f35a4390195dd68502f8dcfddd444f87eea481181fba9",
  "src/pricing-coverage-floor.mjs": "3240da7fdb92d6ac953e70863a2e8983ccaba8a9dc51e3b02b064f6e658f9877",
  "src/program-registry.mjs": "05724dc3d63f6424c772261ebf8f8798421efc7542a3c5d8c5ce9e4c31341bd8",
  "src/provider-cta-contract.mjs": "351dbba3db12fa4cdbe3dca2adf58214444fec12a22cec7d8daee69b4909ec32",
  "src/provider-display-evidence.mjs": "d87875aec1c9c03b5afbf5fc8135b1372fe2667f96221f0ceb8c4b1933eadd91",
  "src/provider-dynamic-profiles.mjs": "f2b900cf740e4336beec31cd0fa4a8776eba2023f161fdc7216409ce5bab8023",
  "src/provider-identity.mjs": "1c1d1078c90b8d528973141422137a6f92e73377c7c503bb136585a6aaa70a80",
  "src/typed-price-labels.mjs": "374b2ebde206e1cdff74ab7c89f069db033365ffceb9bd867ac7229c477848f3",
  "data/go-selection-receipt.json": "712a058818ee5c5c9fd9f030da286fe8979069c7e2dc21a37416ed612c543e7e",
  "data/go-terms-review-ledger.json": "d38d452c71155b33779527944b0b6ba7b156c56a31bba6b212bf4b2cc9d274da",
  "data/go-price-basis-supersession.json": "b2816ef01878619ab4dfa5a30130f5c1422072689dda2eb2d57df4171162e473"
});

async function verifyGoRefreshContract(release) {
  if (await realpath(release) !== release) throw new Error("go_runtime_refresh_root_alias");
  for (const [relative, expected] of Object.entries(GO_REFRESH_CONTRACT)) {
    const file = path.join(release, relative);
    let actual;
    try {
      if (await realpath(file) !== file) throw new Error("root_alias");
      actual = createHash("sha256").update(await readFile(file)).digest("hex");
    } catch { throw new Error(`go_runtime_refresh_contract_drift:${relative}`); }
    if (actual !== expected) throw new Error(`go_runtime_refresh_contract_drift:${relative}`);
  }
}

function parseArgs(argv) {
  const args = { release: "" };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--release") args.release = argv[++index] || "";
    else throw new Error(`unknown_argument:${arg}`);
  }
  if (!args.release) throw new Error("release_required");
  return args;
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function atomicWrite(filePath, text) {
  const temporary = `${filePath}.b2-refresh-${process.pid}`;
  try {
    await writeFile(temporary, text, { encoding: "utf8", mode: 0o644 });
    await rename(temporary, filePath);
  } finally {
    await rm(temporary, { force: true });
  }
}

async function main() {
  const { release: releaseArg } = parseArgs(process.argv.slice(2));
  const release = path.resolve(releaseArg);
  await verifyGoRefreshContract(release);
  const registryPath = path.join(release, "data", "program-registry.json");
  const ledgerPath = path.join(release, "data", "program-content-ledger.json");
  const stagedManifestPath = path.join(release, "artifacts", "ops", "worker-manifest.staging.json");
  const runtimePath = path.join(release, "functions", "go", "program-manifest.js");
  await Promise.all([
    access(registryPath), access(ledgerPath), access(stagedManifestPath), access(runtimePath),
  ]);

  const [
    { deriveGoEntriesFromRegistry },
    { renderGoRuntimeManifestModule, manifestSha256 },
    { loadAffiliateLinkMap },
    { loadGoPromotionAuthority },
  ] = await Promise.all([
    import(pathToFileURL(path.join(release, "src", "program-registry.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "go-runtime-manifest.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "affiliate-link-map.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "go-runtime-selection.mjs")).href),
  ]);
  const [registry, ledger, stagedManifest] = await Promise.all([
    readJson(registryPath), readJson(ledgerPath), readJson(stagedManifestPath),
  ]);
  const siteProfile = "glp-clinic-scout";
  if (registry?.schema !== "site-program-registry-v1") throw new Error("program_registry_invalid");
  if (!registry?.site_profiles?.[siteProfile]) throw new Error(`site_profile_unknown:${siteProfile}`);
  if (!Array.isArray(ledger?.programs)) throw new Error("content_ledger_programs_invalid");
  if (stagedManifest?.schema !== "affiliate-worker-staging-v3-registry") {
    throw new Error("staged_go_manifest_invalid");
  }
  // Render uses the adopted selection overlay as well as the raw network
  // authority. Read the same governed projection as build-staging, not the
  // raw file alone, or selected /go rows are silently re-frozen as held.
  const goPromotionAuthority = loadGoPromotionAuthority(release);

  if (stagedManifest?.site_profile !== siteProfile) throw new Error("staged_go_manifest_profile_drift");
  if (!String(stagedManifest?.internal_fallback || "").startsWith("https://glpclinicscout.com/")) {
    throw new Error("staged_go_manifest_fallback_invalid");
  }

  const manifest = {
    schema: "affiliate-worker-staging-v3-registry",
    program_registry_schema: registry.schema,
    site_profile: siteProfile,
    // Preserve the candidate's governed fallback instead of maintaining a
    // second literal. build-staging re-derives this same manifest and fails
    // closed if these bytes drift.
    internal_fallback: stagedManifest.internal_fallback,
    // DEFECT 2026-09-02 (deploy-fable-20260901). The /go contract support adds a
    // 5th parameter, goPromotionAuthority, to deriveGoEntriesFromRegistry and
    // derives live_link_allowed / promotion_authorized / status from it. This
    // refresh omitted that argument, so it always regenerated the manifest with
    // an empty authority - every entry unpromoted. It runs AFTER
    // apply_go_runtime_promotion_authority, so it silently reverted the whole
    // promotion and the deploy then failed its own gate with
    // go_runtime_promotion_zero_active. Pass the candidate's authority when the
    // file exists; absent it, the {} default preserves the previous behaviour.
    programs: deriveGoEntriesFromRegistry(
      registry,
      siteProfile,
      ledger.programs,
      loadAffiliateLinkMap("compare", siteProfile),
      goPromotionAuthority,
    ),
  };
  const expected = renderGoRuntimeManifestModule(manifest);
  const before = await readFile(runtimePath, "utf8");
  if (before !== expected) await atomicWrite(runtimePath, expected);
  const after = await readFile(runtimePath, "utf8");
  if (after !== expected) throw new Error("go_runtime_manifest_refresh_readback_mismatch");

  process.stdout.write(`${JSON.stringify({
    schema: "glp-go-runtime-manifest-refresh-v1",
    status: before === expected ? "PASS_ALREADY_CURRENT" : "PASS_REFRESHED",
    release,
    manifest_sha256: manifestSha256(manifest),
    program_count: manifest.programs.length,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exitCode = 1;
});
